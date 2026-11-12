"use client";

import Link from "next/link";

import ErrorAlert from "@/components/ErrorAlert";
import StatusBadge from "@/components/StatusBadge";
import { listPurchaseOrders } from "@/lib/api";
import { formatCents, formatDateTime } from "@/lib/format";
import { useLoad } from "@/lib/useLoad";

export default function OrderHistory() {
  // listPurchaseOrders never changes identity, so it can be passed to useLoad directly.
  const { result, reload } = useLoad(listPurchaseOrders);

  if (result.state === "loading") return <p role="status">Loading order history…</p>;
  if (result.state === "error") {
    return (
      <div>
        <ErrorAlert error={result.error} title="Could not load order history." />
        <button type="button" className="btn" onClick={reload}>
          Try again
        </button>
      </div>
    );
  }
  if (result.data.length === 0) {
    return (
      <p className="card">
        No purchase orders yet. <Link href="/">Ask StockPilot to restock</Link> to create your first draft.
      </p>
    );
  }
  return (
    <div className="table-scroll card">
      <table>
        <thead>
          <tr>
            <th>Order</th>
            <th>Supplier</th>
            <th>Status</th>
            <th className="num">Total</th>
            <th>Payment</th>
            <th>Created</th>
          </tr>
        </thead>
        <tbody>
          {result.data.map((po) => (
            <tr key={po.id}>
              <td>
                <Link href={`/orders/${po.id}`}>PO #{po.id}</Link>
              </td>
              <td>{po.supplier.name}</td>
              <td>
                <StatusBadge status={po.status} />
              </td>
              <td className="num">{formatCents(po.subtotal_cents)}</td>
              <td>
                {po.latest_payment
                  ? `${po.latest_payment.status}${po.latest_payment.verified_at ? " · verified" : ""}`
                  : "—"}
              </td>
              <td>{formatDateTime(po.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
