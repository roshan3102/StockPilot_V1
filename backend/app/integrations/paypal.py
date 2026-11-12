"""A small client for the PayPal Orders v2 REST API (Sandbox).

Endpoints used (https://developer.paypal.com/api/orders/v2/):
    POST /v1/oauth2/token                   get an access token (client credentials)
    POST /v2/checkout/orders                create an order         (supports PayPal-Request-Id)
    GET  /v2/checkout/orders/{id}           read an order's current state
    POST /v2/checkout/orders/{id}/capture   move the money           (supports PayPal-Request-Id)

This module only talks HTTP. It knows nothing about purchase orders or the database, and makes
NO decisions about whether a payment is good: it parses responses into the plain facts that
`domain.payment_verification` judges. Every outcome falls into exactly one of three buckets,
which is the key idea for handling failures safely:

  * a normal return value                      PayPal answered and we understood it
  * PayPalApiError (HTTP 4xx)                  PayPal DEFINITELY refused; the action did not happen
  * PayPalUncertainError (timeout, 5xx, 429,   we do NOT KNOW whether the action happened.
    dropped connection, unreadable reply)      Never assume failure; look the order up again.
                                               Retrying with the same PayPal-Request-Id is safe.

Credentials come from the caller (environment variables, via config) and are never logged.
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from app.domain.money import format_cents, parse_amount_to_cents
from app.domain.payment_verification import ObservedCapture, ObservedOrder

logger = logging.getLogger("stockpilot.paypal")

SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"
DEFAULT_TIMEOUT_SECONDS = 15.0
_TOKEN_EXPIRY_MARGIN_SECONDS = 60  # refresh a minute early rather than race the expiry


# --- errors -------------------------------------------------------------------------------


class PayPalError(Exception):
    """Base class for everything this module raises on purpose."""


class PayPalConfigError(PayPalError):
    """Missing credentials, or a URL that is not the PayPal Sandbox."""


class PayPalApiError(PayPalError):
    """PayPal answered with an HTTP 4xx: it refused, and the requested action did not happen.

    `issue` is PayPal's machine-readable reason, e.g. INSTRUMENT_DECLINED, ORDER_NOT_APPROVED,
    ORDER_ALREADY_CAPTURED. `debug_id` is what PayPal support asks for.
    """

    def __init__(self, status_code: int, issue: str, message: str, debug_id: str | None = None):
        super().__init__(f"PayPal HTTP {status_code} {issue}: {message}")
        self.status_code = status_code
        self.issue = issue
        self.message = message
        self.debug_id = debug_id


class PayPalUncertainError(PayPalError):
    """We cannot tell whether PayPal performed the action (timeout, 5xx, 429, unreadable reply)."""


# --- what callers get back ----------------------------------------------------------------


@dataclass(frozen=True)
class CreatedOrder:
    id: str
    status: str
    approve_url: str | None  # where a buyer can approve without the JS SDK (used for manual tests)


class PayPalGateway(Protocol):
    """The three operations the payment service needs. The real client and the test fake both
    satisfy this, which is what lets tests run without any network."""

    def create_order(
        self, *, amount_cents: int, currency: str, reference: str, description: str, request_id: str
    ) -> CreatedOrder: ...

    def get_order(self, order_id: str) -> ObservedOrder: ...

    def capture_order(self, order_id: str, *, request_id: str) -> ObservedOrder: ...


# --- parsing ------------------------------------------------------------------------------


def _link(data: dict[str, Any], rel: str) -> str | None:
    for link in data.get("links") or []:
        if link.get("rel") == rel:
            return link.get("href")
    return None


def _parse_capture(raw: dict[str, Any]) -> ObservedCapture:
    amount = raw["amount"]
    return ObservedCapture(
        id=str(raw["id"]),
        status=str(raw["status"]),
        amount_cents=parse_amount_to_cents(amount["value"]),
        currency=amount["currency_code"],
        custom_id=raw.get("custom_id"),
        status_reason=(raw.get("status_details") or {}).get("reason"),
    )


def _parse_order(data: dict[str, Any]) -> ObservedOrder:
    try:
        units = data.get("purchase_units") or []
        unit = units[0] if units else {}
        amount = unit["amount"]
        raw_captures = (unit.get("payments") or {}).get("captures") or []
        return ObservedOrder(
            id=str(data["id"]),
            status=str(data["status"]),
            intent=data.get("intent"),
            unit_count=len(units),
            reference_id=unit.get("reference_id"),
            custom_id=unit.get("custom_id"),
            amount_cents=parse_amount_to_cents(amount["value"]),
            currency=amount["currency_code"],
            captures=tuple(_parse_capture(c) for c in raw_captures),
            approve_url=_link(data, "approve") or _link(data, "payer-action"),
        )
    except (KeyError, IndexError, TypeError, AttributeError, ValueError) as exc:
        raise PayPalUncertainError("PayPal sent a reply we could not read") from exc


def _require_sandbox(base_url: str) -> None:
    parsed = urlparse(base_url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not (host == "sandbox.paypal.com" or host.endswith(".sandbox.paypal.com")):
        raise PayPalConfigError(
            "PAYPAL_BASE_URL must be an https PayPal Sandbox URL; live PayPal is disabled in the MVP"
        )


def _check_order_id(order_id: str) -> None:
    # PayPal order ids are 1-36 characters of A-Z and 0-9. Refusing anything else keeps odd
    # input (slashes, "..") from ever becoming part of a URL path.
    if not (1 <= len(order_id) <= 36 and order_id.isascii() and order_id.isalnum() and order_id.upper() == order_id):
        raise ValueError(f"not a PayPal order id: {order_id!r}")


# --- the client ---------------------------------------------------------------------------


class PayPalClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str = SANDBOX_BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ):
        """`transport` exists for tests: an `httpx.MockTransport` answers requests in memory."""
        if not client_id or not client_secret:
            raise PayPalConfigError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must both be set")
        _require_sandbox(base_url)
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_expires_at = 0.0

    def __repr__(self) -> str:  # deliberately omits the secret
        return f"PayPalClient(base_url={str(self._http.base_url)!r}, client_id={self._client_id[:6]}...)"

    def close(self) -> None:
        self._http.close()

    # --- public operations ---

    def create_order(
        self, *, amount_cents: int, currency: str, reference: str, description: str, request_id: str
    ) -> CreatedOrder:
        """Create a CAPTURE-intent order for one purchase unit. The amount is formatted from
        integer cents here and nowhere else. `request_id` makes a retry return the SAME order."""
        payload = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": reference,
                "custom_id": reference,
                "description": description,
                "amount": {"currency_code": currency, "value": format_cents(amount_cents)},
            }],
        }
        data = self._call("POST", "/v2/checkout/orders", json=payload, request_id=request_id)
        try:
            return CreatedOrder(
                id=str(data["id"]), status=str(data["status"]),
                approve_url=_link(data, "approve") or _link(data, "payer-action"),
            )
        except (KeyError, TypeError) as exc:
            raise PayPalUncertainError("PayPal sent a reply we could not read") from exc

    def get_order(self, order_id: str) -> ObservedOrder:
        _check_order_id(order_id)
        return _parse_order(self._call("GET", f"/v2/checkout/orders/{order_id}"))

    def capture_order(self, order_id: str, *, request_id: str) -> ObservedOrder:
        """Ask PayPal to move the money. The body is empty on purpose: no `payment_source` means
        PayPal will only capture an order the buyer has already approved. `return=representation`
        makes PayPal send the amounts back (the default reply has only id and status)."""
        _check_order_id(order_id)
        data = self._call(
            "POST", f"/v2/checkout/orders/{order_id}/capture",
            json={}, request_id=request_id, representation=True,
        )
        return _parse_order(data)

    # --- plumbing ---

    def _call(
        self, method: str, path: str, *, json: dict | None = None,
        request_id: str | None = None, representation: bool = False,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        if representation:
            headers["Prefer"] = "return=representation"
        for attempt in (1, 2):
            headers["Authorization"] = f"Bearer {self._access_token()}"
            response = self._send(method, path, json=json, headers=headers)
            if response.status_code == 401 and attempt == 1:
                self._forget_token()  # expired or revoked: get a fresh one and try once more
                continue
            return self._read(response)
        raise AssertionError("unreachable")  # pragma: no cover

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:  # timeouts, refused/dropped connections, protocol errors
            raise PayPalUncertainError(
                f"could not complete the request to PayPal ({type(exc).__name__})"
            ) from exc

    def _read(self, response: httpx.Response) -> dict[str, Any]:
        code = response.status_code
        if code >= 500 or code == 429:
            raise PayPalUncertainError(f"PayPal answered HTTP {code}")
        if 200 <= code < 300:
            try:
                body = response.json()
            except ValueError as exc:
                raise PayPalUncertainError("PayPal sent a reply that is not JSON") from exc
            if not isinstance(body, dict):
                raise PayPalUncertainError("PayPal sent an unexpected reply")
            return body
        raise self._api_error(response)

    @staticmethod
    def _api_error(response: httpx.Response) -> PayPalApiError:
        try:
            body = response.json()
            if not isinstance(body, dict):
                body = {}
        except ValueError:
            body = {}
        details = body.get("details") or [{}]
        first = details[0] if isinstance(details[0], dict) else {}
        # Orders errors carry `details[0].issue`; OAuth errors use `error`.
        issue = first.get("issue") or body.get("name") or body.get("error") or "UNKNOWN"
        message = first.get("description") or body.get("message") or body.get("error_description") or ""
        debug_id = body.get("debug_id") or response.headers.get("paypal-debug-id")
        logger.warning("PayPal HTTP %s %s (debug_id=%s)", response.status_code, issue, debug_id)
        return PayPalApiError(response.status_code, str(issue), str(message), debug_id)

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token
            response = self._send(
                "POST", "/v1/oauth2/token",
                auth=(self._client_id, self._client_secret),
                data={"grant_type": "client_credentials"},
                headers={"Accept": "application/json"},
            )
            body = self._read(response)
            try:
                token = str(body["access_token"])
                lifetime = float(body.get("expires_in", 0))
            except (KeyError, TypeError, ValueError) as exc:
                raise PayPalUncertainError("PayPal sent a token reply we could not read") from exc
            self._token = token
            self._token_expires_at = time.monotonic() + max(lifetime - _TOKEN_EXPIRY_MARGIN_SECONDS, 0)
            return token

    def _forget_token(self) -> None:
        with self._lock:
            self._token = None
