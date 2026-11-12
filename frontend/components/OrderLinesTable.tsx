import type { PurchaseOrder } from "@/lib/api";
import { formatCases, formatCents } from "@/lib/format";

export default function OrderLinesTable({ po }: { po: PurchaseOrder }) {
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Product</th>
            <th className="num">Units</th>
            <th className="num">Cases</th>
            <th className="num">Unit price</th>
            <th className="num">Line total</th>
            <th>AI reason</th>
          </tr>
        </thead>
        <tbody>
          {po.lines.map((line) => (
            <tr key={line.sku}>
              <td>
                {line.name} <code>{line.sku}</code>
              </td>
              <td className="num">{line.quantity}</td>
              <td className="num">{formatCases(line.quantity, line.pack_size)}</td>
              <td className="num">{formatCents(line.unit_price_cents)}</td>
              <td className="num">{formatCents(line.line_total_cents)}</td>
              <td>{line.reason ?? "—"}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <th colSpan={4}>Total (calculated by the server)</th>
            <td className="num">
              <strong data-testid="po-total">{formatCents(po.subtotal_cents)}</strong>
            </td>
            <td />
          </tr>
        </tfoot>
      </table>
    </div>
  );
}
