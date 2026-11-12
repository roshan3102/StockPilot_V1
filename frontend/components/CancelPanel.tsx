"use client";

import { useState } from "react";

import ErrorAlert, { describeError } from "@/components/ErrorAlert";
import { ApiError, cancelPurchaseOrder, type PurchaseOrder } from "@/lib/api";

const MAX_REASON_CHARS = 500; // matches CancelPurchaseOrderRequest.reason in the backend

/** Reject a DRAFT, or cancel an APPROVED order before payment starts. Asks for confirmation. */
export default function CancelPanel({
  po,
  onCancelled,
  onConflict,
}: {
  po: PurchaseOrder;
  onCancelled: (po: PurchaseOrder) => void;
  onConflict: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const verb = po.status === "DRAFT" ? "Reject draft" : "Cancel order";

  async function submit() {
    setSubmitting(true);
    setError(null);
    try {
      onCancelled(await cancelPurchaseOrder(po.id, reason.trim() || null));
    } catch (err) {
      setError(err);
      if (err instanceof ApiError && err.code === "PO_NOT_CANCELLABLE") await onConflict();
    } finally {
      setSubmitting(false);
    }
  }

  if (!open) {
    return (
      <button type="button" className="btn btn-danger" onClick={() => setOpen(true)}>
        {verb}
      </button>
    );
  }
  return (
    <div className="confirm" role="group" aria-label={verb}>
      <label htmlFor="cancel-reason">Reason (optional, kept in the order record)</label>
      <textarea
        id="cancel-reason"
        rows={2}
        maxLength={MAX_REASON_CHARS}
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        disabled={submitting}
      />
      <div className="actions">
        <button type="button" className="btn btn-danger" onClick={submit} disabled={submitting}>
          {submitting ? "Working…" : `Confirm: ${verb.toLowerCase()}`}
        </button>
        <button type="button" className="btn" onClick={() => setOpen(false)} disabled={submitting}>
          Keep order
        </button>
      </div>
      {error !== null && (
        <ErrorAlert error={error} title={describeError(error) ?? "The order was not changed."} />
      )}
    </div>
  );
}
