import type { ProductQuotes } from "@/lib/runTrace";
import { formatCents } from "@/lib/format";

/** The supplier options the agent compared, per product. Read-only: these are the quotes the
 * backend returned to the agent's `get_supplier_quotes` tool, straight from supplier data. */
export default function SupplierComparison({
  products,
  chosen,
}: {
  products: ProductQuotes[];
  chosen: Map<string, number>; // sku -> supplier id on a draft PO
}) {
  if (products.length === 0) {
    return <p className="hint">The assistant did not compare suppliers in this run.</p>;
  }
  return (
    <div className="stack">
      {products.map((product) => (
        <div key={product.sku}>
          <h4>
            {product.name} <code>{product.sku}</code>{" "}
            <span className="hint">short by {product.shortfall_units} units</span>
          </h4>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Supplier</th>
                  <th className="num">Unit price</th>
                  <th className="num">Case</th>
                  <th className="num">Supplier minimum</th>
                  <th className="num">Lead time</th>
                  <th>To cover the shortfall</th>
                </tr>
              </thead>
              <tbody>
                {product.offers.map((offer) => {
                  const isChosen = chosen.get(product.sku) === offer.supplier_id;
                  return (
                    <tr key={offer.supplier_id} className={isChosen ? "row-chosen" : undefined}>
                      <td>
                        {offer.supplier_name}
                        {isChosen && <span className="badge badge-chosen">on draft</span>}
                      </td>
                      <td className="num">{formatCents(offer.unit_price_cents)}</td>
                      <td className="num">{offer.pack_size}</td>
                      <td className="num">{formatCents(offer.supplier_min_order_cents)}</td>
                      <td className="num">{offer.lead_time_days} days</td>
                      <td>
                        {offer.quote_for_shortfall
                          ? `${offer.quote_for_shortfall.quantity} units for ${formatCents(
                              offer.quote_for_shortfall.line_total_cents,
                            )}${offer.quote_for_shortfall.capped_by_max_stock ? " (capped by max stock)" : ""}`
                          : (offer.unusable_because ?? "—")}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </div>
  );
}
