"use client";

import { useState } from "react";

import ErrorAlert, { describeError } from "@/components/ErrorAlert";
import { ApiError, approvePurchaseOrder, type PurchaseOrder } from "@/lib/api";
import { formatCents } from "@/lib/format";

// Codes that mean "what you are looking at is out of date": reload so the merchant re-reviews.
const RELOAD_CODES = new Set(["STALE_DRAFT", "TOTAL_MISMATCH", "PRICES_CHANGED", "PO_NOT_APPROVABLE"]);

/** Two-step approval: the merchant must click "Approve" and then confirm the exact total. The
 * request carries the server's total and version that are on screen, so the backend can refuse
 * if anything changed in between (409) instead of approving a number nobody saw. */
export default function ApprovePanel({
  po,
  onApproved,
  onConflict,
}: {
  po: PurchaseOrder;
  onApproved: (po: PurchaseOrder) => void;
  onConflict: () => Promise<void>;
}) {
  const [confirming, setConfirming] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const total = formatCents(po.subtotal_cents);

  async function approve() {
    setSubmitting(true);
    setError(null);
    try {
      onApproved(await approvePurchaseOrder(po.id, po.subtotal_cents, po.version));
    } catch (err) {
      setError(err);
      setConfirming(false);
      if (err instanceof ApiError && RELOAD_CODES.has(err.code)) await onConflict();
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div>
      {!confirming ? (
        <button type="button" className="btn btn-primary" onClick={() => setConfirming(true)}>
          Approve {total}
        </button>
      ) : (
        <div className="confirm" role="group" aria-label="Confirm approval">
          <p>
            Approve PO #{po.id} to <strong>{po.supplier.name}</strong> for <strong>{total}</strong>?
            After approval the order is frozen and you can pay for it with PayPal.
          </p>
          <div className="actions">
            <button type="button" className="btn btn-primary" onClick={approve} disabled={submitting}>
              {submitting ? "Approving…" : `Confirm approval of ${total}`}
            </button>
            <button type="button" className="btn" onClick={() => setConfirming(false)} disabled={submitting}>
              Back
            </button>
          </div>
        </div>
      )}
      {error !== null && (
        <ErrorAlert error={error} title={describeError(error) ?? "The order was not approved."} />
      )}
    </div>
  );
}
