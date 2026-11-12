"use client";

import { useState } from "react";

import ErrorAlert from "@/components/ErrorAlert";
import PayPalButtons from "@/components/PayPalButtons";
import { ApiError, capturePayPalOrder, type Payment, type PurchaseOrder } from "@/lib/api";
import { formatCents, formatDateTime } from "@/lib/format";

type Notice = { kind: "info" | "warning" | "error"; text: string };

/** What the merchant should be told after a capture attempt the server did NOT confirm as paid.
 * Successful payments get no notice here: the refreshed PO (status PAID) is the only proof. */
function noticeFor(error: unknown): Notice | null {
  if (!(error instanceof ApiError)) return null;
  switch (error.code) {
    case "PAYMENT_DECLINED":
      return { kind: "error", text: "PayPal declined the payment. Nothing was charged. You can try again." };
    case "VERIFICATION_FAILED":
      return {
        kind: "error",
        text: "PayPal's record did not match this order, so it was NOT marked paid. It has been flagged for review.",
      };
    case "PAYPAL_UNAVAILABLE":
      return {
        kind: "warning",
        text: "PayPal did not give a clear answer, so the outcome is unknown. Use “Check payment status” to ask again; you will not be charged twice.",
      };
    case "BUYER_APPROVAL_REQUIRED":
      return { kind: "warning", text: "PayPal has not received your approval yet. Complete checkout first." };
    default:
      return null;
  }
}

/** The verified payment behind a PAID order: CAPTURED and checked against PayPal's records. */
export function verifiedPayment(po: PurchaseOrder): Payment | undefined {
  if (po.status !== "PAID") return undefined;
  return po.payments.find((p) => p.status === "CAPTURED" && p.verified_at !== null);
}

/** Everything about paying for one PO. Every status shown here comes from the backend's copy of
 * the order (`po`), refreshed after each step: returning from PayPal proves nothing by itself. */
export default function PaymentPanel({
  po,
  onRefresh,
}: {
  po: PurchaseOrder;
  onRefresh: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [error, setError] = useState<unknown>(null);
  const latest = po.payments.at(-1);

  async function verify(paypalOrderId: string) {
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      const result = await capturePayPalOrder(po.id, paypalOrderId);
      if (result.outcome === "PENDING") {
        setNotice({ kind: "info", text: "PayPal accepted the payment but has not finalised it yet. It is not paid yet." });
      }
    } catch (err) {
      const known = noticeFor(err);
      if (known) setNotice(known);
      else setError(err);
    } finally {
      // Whatever happened, show the server's current record of the order.
      try {
        await onRefresh();
      } catch (err) {
        setError(err);
      }
      setBusy(false);
    }
  }

  const banners = (
    <>
      {busy && <p role="status">Verifying the payment with the server…</p>}
      {notice && (
        <p role="alert" className={`alert alert-${notice.kind}`}>
          {notice.text}
        </p>
      )}
      {error !== null && <ErrorAlert error={error} title="Payment problem." />}
    </>
  );

  if (po.status === "DRAFT") {
    return <p className="hint">Payment becomes available after you approve this order.</p>;
  }
  if (po.status === "CANCELLED") {
    return <p className="hint">This order was cancelled. No payment can be made.</p>;
  }
  if (po.status === "PAID") {
    const payment = verifiedPayment(po);
    if (!payment) {
      return (
        <p role="alert" className="alert alert-error">
          The order is marked paid but no verified payment record was found. Contact support.
        </p>
      );
    }
    return <Receipt po={po} payment={payment} />;
  }

  // PAYMENT_PENDING with a capture PayPal has not finalised (or one flagged for review).
  if (po.status === "PAYMENT_PENDING" && latest && latest.status === "PENDING") {
    return (
      <div className="stack">
        <p className="alert alert-warning">
          {latest.error
            ? `Needs review: ${latest.error}`
            : "PayPal is still processing this payment. The order is not paid yet."}
        </p>
        <button type="button" className="btn" disabled={busy} onClick={() => verify(latest.paypal_order_id)}>
          Check payment status
        </button>
        {banners}
      </div>
    );
  }

  // APPROVED, PAYMENT_FAILED (retry with a new PayPal order), or PAYMENT_PENDING with an open
  // order the buyer has not finished (the backend resumes the SAME order, so no double charge).
  const openOrder = po.status === "PAYMENT_PENDING" && latest?.status === "CREATED" ? latest : null;
  return (
    <div className="stack">
      <p>
        Amount to pay: <strong>{formatCents(po.approved_total_cents ?? po.subtotal_cents)}</strong>{" "}
        <span className="hint">(the approved total, sent to PayPal by the server)</span>
      </p>
      {po.status === "PAYMENT_FAILED" && latest?.error && (
        <p className="alert alert-error">Previous attempt failed: {latest.error}. You can try again.</p>
      )}
      {openOrder && (
        <p className="alert alert-info">
          A checkout was started but not completed. Continue below to resume it, or check its status
          if you already approved it in PayPal.
        </p>
      )}
      {banners}
      {/* Hidden, not unmounted, while verifying: PayPal is still inside its onApprove callback. */}
      <div hidden={busy}>
        <PayPalButtons
          poId={po.id}
          onApproved={verify}
          onCancelled={() => {
            setNotice({ kind: "info", text: "PayPal checkout was closed. Nothing was charged; you can resume at any time." });
            onRefresh().catch(setError);
          }}
          onError={(err) => {
            setError(err);
            onRefresh().catch(setError);
          }}
        />
      </div>
      {openOrder && (
        <button type="button" className="btn" disabled={busy} onClick={() => verify(openOrder.paypal_order_id)}>
          Check payment status
        </button>
      )}
    </div>
  );
}

function Receipt({ po, payment }: { po: PurchaseOrder; payment: Payment }) {
  return (
    <div className="receipt" aria-label="Payment receipt">
      <p className="alert alert-success">
        <strong>Payment verified.</strong> The server confirmed the capture with PayPal and checked the
        amount, currency and order reference.
      </p>
      <dl>
        <dt>Amount</dt>
        <dd>{formatCents(payment.amount_cents)} {payment.currency}</dd>
        <dt>Supplier</dt>
        <dd>{po.supplier.name}</dd>
        <dt>PayPal order</dt>
        <dd><code>{payment.paypal_order_id}</code></dd>
        <dt>PayPal capture</dt>
        <dd><code>{payment.paypal_capture_id}</code></dd>
        <dt>Verified at</dt>
        <dd>{formatDateTime(payment.verified_at!)}</dd>
      </dl>
    </div>
  );
}
