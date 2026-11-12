import type { PurchaseOrder } from "@/lib/api";
import { formatCents, formatDateTime } from "@/lib/format";

/** The audit trail: every payment attempt and every recorded event, as stored by the backend. */
export default function TransactionRecord({ po }: { po: PurchaseOrder }) {
  return (
    <div className="stack">
      <h3>Payment attempts</h3>
      {po.payments.length === 0 ? (
        <p className="hint">No payment attempts yet.</p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>PayPal order</th>
                <th>Status</th>
                <th className="num">Amount</th>
                <th>Verified</th>
                <th>Notes</th>
                <th>Started</th>
              </tr>
            </thead>
            <tbody>
              {po.payments.map((p) => (
                <tr key={p.id}>
                  <td><code>{p.paypal_order_id}</code></td>
                  <td>{p.status}</td>
                  <td className="num">{formatCents(p.amount_cents)}</td>
                  <td>{p.verified_at ? formatDateTime(p.verified_at) : "—"}</td>
                  <td>{p.error ?? "—"}</td>
                  <td>{formatDateTime(p.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h3>Event log</h3>
      <ol className="trace">
        {po.events.map((e) => (
          <li key={e.id}>
            <details>
              <summary>
                {formatDateTime(e.at)} · <strong>{e.event}</strong>
              </summary>
              <pre>{JSON.stringify(e.detail, null, 2)}</pre>
            </details>
          </li>
        ))}
      </ol>
    </div>
  );
}
