"use client";

import Link from "next/link";
import { useCallback, useState } from "react";

import ApprovePanel from "@/components/ApprovePanel";
import CancelPanel from "@/components/CancelPanel";
import DraftEditor from "@/components/DraftEditor";
import ErrorAlert from "@/components/ErrorAlert";
import OrderLinesTable from "@/components/OrderLinesTable";
import PaymentPanel from "@/components/PaymentPanel";
import RebateNote from "@/components/RebateNote";
import StatusBadge from "@/components/StatusBadge";
import TransactionRecord from "@/components/TransactionRecord";
import { ApiError, getPurchaseOrder, type PurchaseOrder } from "@/lib/api";
import { formatCents, formatDateTime } from "@/lib/format";
import { useLoad } from "@/lib/useLoad";

/** The review page for one PO. It owns the single copy of the order and replaces it with the
 * server's response after every action, so the screen always shows the backend's numbers. */
export default function PurchaseOrderView({ poId }: { poId: number }) {
  const load = useCallback(() => getPurchaseOrder(poId), [poId]);
  const { result, setData } = useLoad(load);
  const [editing, setEditing] = useState(false);
  const [refreshError, setRefreshError] = useState<unknown>(null);

  const refresh = useCallback(async () => {
    try {
      setData(await getPurchaseOrder(poId));
      setRefreshError(null);
    } catch (err) {
      setRefreshError(err);
      throw err;
    }
  }, [poId, setData]);

  if (result.state === "loading") return <p role="status">Loading purchase order…</p>;
  if (result.state === "error") {
    if (result.error instanceof ApiError && result.error.status === 404) {
      return <p role="alert">There is no purchase order #{poId}.</p>;
    }
    return <ErrorAlert error={result.error} title="Could not load this order." />;
  }

  const po = result.data;
  const replace = (fresh: PurchaseOrder) => {
    setEditing(false);
    setData(fresh);
  };
  const reloadQuietly = () => refresh().catch(() => {});

  return (
    <div className="stack">
      <section className="card">
        <header className="card-header">
          <h2>
            PO #{po.id} · {po.supplier.name}
          </h2>
          <StatusBadge status={po.status} />
        </header>
        <p className="hint">
          Created {formatDateTime(po.created_at)} · version {po.version}
          {po.budget_cents !== null && <> · run budget {formatCents(po.budget_cents)}</>}
          {po.run_id !== null && (
            <>
              {" "}· <Link href={`/runs/${po.run_id}`}>from restock run #{po.run_id}</Link>
            </>
          )}
        </p>
        {po.rationale && (
          <p>
            <span className="ai-label">AI rationale:</span> {po.rationale}
          </p>
        )}
        {po.promotion && <RebateNote promotion={po.promotion} subtotalCents={po.subtotal_cents} />}
        {refreshError !== null && <ErrorAlert error={refreshError} title="Could not refresh the order." />}
      </section>

      <section className="card">
        <h3>Order lines</h3>
        {editing && po.status === "DRAFT" ? (
          <DraftEditor po={po} onSaved={replace} onCancel={() => setEditing(false)} onStale={reloadQuietly} />
        ) : (
          <>
            <OrderLinesTable po={po} />
            {po.status === "DRAFT" && (
              <div className="actions">
                <button type="button" className="btn" onClick={() => setEditing(true)}>
                  Edit order
                </button>
                <ApprovePanel po={po} onApproved={replace} onConflict={reloadQuietly} />
                <CancelPanel po={po} onCancelled={replace} onConflict={reloadQuietly} />
              </div>
            )}
            {po.status === "APPROVED" && (
              <div className="actions">
                <span className="hint">
                  Approved {po.approved_at && formatDateTime(po.approved_at)} for{" "}
                  {formatCents(po.approved_total_cents ?? po.subtotal_cents)}.
                </span>
                <CancelPanel po={po} onCancelled={replace} onConflict={reloadQuietly} />
              </div>
            )}
          </>
        )}
      </section>

      <section className="card">
        <h3>Payment</h3>
        <PaymentPanel po={po} onRefresh={refresh} />
      </section>

      <section className="card">
        <h3>Transaction record</h3>
        <TransactionRecord po={po} />
      </section>
    </div>
  );
}
