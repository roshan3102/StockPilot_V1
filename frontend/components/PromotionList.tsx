"use client";

import Link from "next/link";

import ErrorAlert from "@/components/ErrorAlert";
import { listPromotions } from "@/lib/api";
import { formatCents } from "@/lib/format";
import { useLoad } from "@/lib/useLoad";

export default function PromotionList() {
  const { result, reload } = useLoad(listPromotions);

  if (result.state === "loading") return <p role="status">Loading promotions…</p>;
  if (result.state === "error") {
    return (
      <div>
        <ErrorAlert error={result.error} title="Could not load promotions." />
        <button type="button" className="btn" onClick={reload}>
          Try again
        </button>
      </div>
    );
  }
  if (result.data.length === 0) return <p className="card">No promotions yet. Add one below.</p>;
  return (
    <div className="table-scroll card">
      <table>
        <thead>
          <tr>
            <th>Promotion</th>
            <th>Supplier</th>
            <th className="num">Cases</th>
            <th className="num">Rebate</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {result.data.map((p) => (
            <tr key={p.id}>
              <td>
                <Link href={`/promotions/${p.id}`}>{p.title}</Link>
              </td>
              <td>{p.supplier.name}</td>
              <td className="num">{p.required_cases}</td>
              <td className="num">{formatCents(p.rebate_cents)}</td>
              <td>{p.unavailable_reason ?? "Active"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
