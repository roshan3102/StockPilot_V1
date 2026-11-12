"""The one place that talks to the LLM provider.

The rest of the agent depends on the tiny `LLMClient` protocol below, never on the OpenAI SDK.
That gives us two things:

* Tests plug in a scripted fake that implements the same protocol, so the core test suite
  never needs a network connection, an API key, or a real model.
* If the provider changes, only `OpenAIChatClient` changes.

Everything that can go wrong with the provider (timeout, rate limit, bad key, unknown model,
garbled reply) is converted into `LLMError` with a stable `code` and a message that is safe
to show to a merchant. The message never contains the API key or the raw provider response.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import openai

from app.config import Settings, get_settings

# Messages and tool schemas use the OpenAI "chat" shapes as plain dicts, e.g.
#   {"role": "user", "content": "..."}  and  {"type": "function", "function": {...}}
# Keeping them as dicts means the loop and the tests need no SDK types.
Message = dict[str, Any]
ToolSchema = dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """The model asking us to run one tool. `arguments` is the raw JSON TEXT the model wrote:
    it is untrusted input and is parsed and validated in tools.py, not here."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class LLMReply:
    """One model turn: some text, some tool calls, or both. No tool calls means "I'm done"."""

    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()


class LLMError(Exception):
    """The model could not be used. `code` is stable for programs; `str(error)` is for people.

    `detail` is for developers only (exception type, HTTP status, the provider's own
    explanation, with anything key-shaped redacted). It is never part of `str(error)`, so it
    cannot reach a merchant by accident; the trace and the dev runner show it.
    """

    def __init__(self, code: str, message: str, detail: str | None = None):
        self.code = code
        self.detail = detail
        super().__init__(message)


class LLMClient(Protocol):
    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSchema]) -> LLMReply:
        """Send the conversation and tool schemas; return the model's next turn.
        Raises LLMError for any provider problem; never anything provider-specific."""
        ...


class StructuredLLMClient(Protocol):
    def complete_json(
        self, messages: Sequence[Message], *, name: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        """One reply constrained to `schema` (a strict JSON Schema), parsed. The result is
        still untrusted: callers validate it like any other input. Raises LLMError."""
        ...


class OpenAIChatClient:
    """`LLMClient` backed by the OpenAI Chat Completions API (function calling)."""

    def __init__(
        self, *, api_key: str, model: str, timeout_seconds: float,
        reasoning_effort: str = "", client: Any = None,
    ):
        self._model = model
        self._extra: dict[str, Any] = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
        # `client` lets a test inject a fake SDK object. The SDK retries transient failures
        # (connection errors, 429, 5xx) twice by default; that is the only retrying we do.
        self._client = client or openai.OpenAI(api_key=api_key, timeout=timeout_seconds)

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSchema]) -> LLMReply:
        try:
            response = self._client.chat.completions.create(
                model=self._model, messages=list(messages), tools=list(tools), **self._extra
            )
        except openai.OpenAIError as exc:
            raise _translate(exc) from exc
        return _parse(response)

    def complete_json(
        self, messages: Sequence[Message], *, name: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            response = self._client.chat.completions.create(
                model=self._model, messages=list(messages), **self._extra,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": name, "strict": True, "schema": schema},
                },
            )
        except openai.OpenAIError as exc:
            raise _translate(exc) from exc
        content = _parse(response).content
        try:
            data = json.loads(content or "")
        except json.JSONDecodeError as exc:
            raise LLMError("LLM_BAD_RESPONSE", "The AI service sent a reply we could not read.") from exc
        if not isinstance(data, dict):
            raise LLMError("LLM_BAD_RESPONSE", "The AI service sent a reply we could not read.")
        return data


_KEY_SHAPED = re.compile(r"sk-[A-Za-z0-9_\-*]+")


def _detail(exc: openai.OpenAIError) -> str:
    """Developer-facing description of an SDK error, safe to log: key-like text is redacted
    and the length is capped."""
    status = getattr(exc, "status_code", None)
    text = _KEY_SHAPED.sub("sk-REDACTED", str(exc))[:500]
    return f"{type(exc).__name__}" + (f" (HTTP {status})" if status else "") + f": {text}"


def _translate(exc: openai.OpenAIError) -> LLMError:
    """Map SDK exceptions to our own. Order matters: APITimeoutError is a kind of
    APIConnectionError, so it must be checked first."""
    code, message = _classify(exc)
    return LLMError(code, message, _detail(exc))


def _classify(exc: openai.OpenAIError) -> tuple[str, str]:
    if isinstance(exc, openai.APITimeoutError):
        return "LLM_TIMEOUT", "The AI service took too long to respond."
    if isinstance(exc, openai.APIConnectionError):
        return "LLM_UNREACHABLE", "Could not reach the AI service."
    if isinstance(exc, openai.RateLimitError):
        return "LLM_RATE_LIMITED", "The AI service is busy or over its usage limit."
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return "LLM_AUTH", "The AI service rejected the configured credentials."
    if isinstance(exc, openai.NotFoundError):
        return "LLM_MODEL_NOT_FOUND", "The configured AI model is not available."
    return "LLM_UNAVAILABLE", "The AI service returned an error."


def _parse(response: Any) -> LLMReply:
    """Turn an SDK response into an `LLMReply`, refusing shapes we do not understand."""
    try:
        message = response.choices[0].message
        calls = tuple(
            ToolCall(id=call.id, name=call.function.name, arguments=call.function.arguments)
            for call in (message.tool_calls or [])
            if call.type == "function"
        )
        content = message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise LLMError("LLM_BAD_RESPONSE", "The AI service sent a reply we could not read.") from exc
    return LLMReply(content=content, tool_calls=calls)


def get_llm_client(settings: Settings | None = None) -> OpenAIChatClient:
    """Build the real client from environment settings. Raises LLMError if no key is set."""
    settings = settings or get_settings()
    api_key = settings.openai_api_key.get_secret_value()
    if not api_key:
        raise LLMError("LLM_NOT_CONFIGURED", "No AI service is configured (OPENAI_API_KEY is empty).")
    return OpenAIChatClient(
        api_key=api_key, model=settings.llm_model, timeout_seconds=settings.llm_timeout_seconds,
        reasoning_effort=settings.llm_reasoning_effort,
    )

