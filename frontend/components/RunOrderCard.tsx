import Link from "next/link";

import RebateNote from "@/components/RebateNote";
import StatusBadge from "@/components/StatusBadge";
import type { RunPurchaseOrder } from "@/lib/api";
import { formatCases, formatCents } from "@/lib/format";

/** One draft PO from a run. Every number is from the database; only `reason` and
 * `rationale` are the AI's words, and they are labelled as such. */
export default function RunOrderCard({ order }: { order: RunPurchaseOrder }) {
  return (
    <article className="card" aria-labelledby={`po-${order.id}-title`}>
      <header className="card-header">
        <h3 id={`po-${order.id}-title`}>
          PO #{order.id} · {order.supplier.name}
        </h3>
        <StatusBadge status={order.status} />
      </header>
      {order.rationale && (
        <p>
          <span className="ai-label">AI rationale:</span> {order.rationale}
        </p>
      )}
      {order.promotion && (
        <RebateNote promotion={order.promotion} subtotalCents={order.subtotal_cents} />
      )}

      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Product</th>
              <th className="num">Units</th>
              <th className="num">Cases</th>
              <th className="num">Unit price</th>
              <th className="num">Line total</th>
              <th>Evidence (database)</th>
              <th>AI reason</th>
            </tr>
          </thead>
          <tbody>
            {order.lines.map((line) => (
              <tr key={line.sku}>
                <td>
                  {line.name} <code>{line.sku}</code>
                </td>
                <td className="num">{line.quantity}</td>
                <td className="num">{formatCases(line.quantity, line.pack_size)}</td>
                <td className="num">{formatCents(line.unit_price_cents)}</td>
                <td className="num">{formatCents(line.line_total_cents)}</td>
                <td className="evidence">
                  {line.evidence.on_hand} on hand · {line.evidence.avg_daily_sales}/day ·{" "}
                  {line.evidence.days_of_cover === null
                    ? "no recent sales"
                    : `${line.evidence.days_of_cover} days of cover`}
                </td>
                <td>{line.reason ?? "—"}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <th colSpan={4}>Subtotal (server)</th>
              <td className="num">
                <strong>{formatCents(order.subtotal_cents)}</strong>
              </td>
              <td colSpan={2} />
            </tr>
          </tfoot>
        </table>
      </div>

      <Link className="btn btn-primary" href={`/orders/${order.id}`}>
        {order.status === "DRAFT" ? "Review, edit or approve" : "View order"}
      </Link>
    </article>
  );
}
