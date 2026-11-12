import type { ProductStock } from "@/lib/api";
import { formatSize } from "@/lib/units";

/** Stock numbers straight from the lookup tool's results, i.e. from the database. */
export default function ProductStockTable({ products }: { products: ProductStock[] }) {
  return (
    <div className="table-scroll">
      <table>
        <caption className="hint">Stock from the database</caption>
        <thead>
          <tr>
            <th>Product</th>
            <th className="num">Size</th>
            <th className="num">On hand</th>
            <th className="num">On order</th>
            <th className="num">Sold/day</th>
            <th className="num">Days of cover</th>
          </tr>
        </thead>
        <tbody>
          {products.map((p) => (
            <tr key={p.sku}>
              <td>
                {p.name} <code>{p.sku}</code>
              </td>
              <td className="num">{formatSize(p.size_ml)}</td>
              <td className="num">{p.on_hand}</td>
              <td className="num">{p.on_order}</td>
              <td className="num">{p.avg_daily_sales}</td>
              <td className="num">{p.days_of_cover ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
