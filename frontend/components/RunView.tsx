"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo } from "react";

import AgentTrace from "@/components/AgentTrace";
import ErrorAlert from "@/components/ErrorAlert";
import ProductStockTable from "@/components/ProductStockTable";
import RunOrderCard from "@/components/RunOrderCard";
import SupplierComparison from "@/components/SupplierComparison";
import { ApiError, getRestockRun, type RestockRunDetail, type RunPurchaseOrder } from "@/lib/api";
import { formatCents, formatDateTime } from "@/lib/format";
import { extractSupplierQuotes } from "@/lib/runTrace";
import { useLoad } from "@/lib/useLoad";

export default function RunView({ runId }: { runId: number }) {
  // useCallback keeps the same function between renders, so useLoad does not refetch forever.
  const load = useCallback(() => getRestockRun(runId), [runId]);
  const { result, reload } = useLoad(load);

  useEffect(() => {
    function onVisible() {
      if (document.visibilityState === "visible") reload();
    }
    window.addEventListener("focus", reload);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener("focus", reload);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [reload]);

  if (result.state === "loading") return <p role="status">Loading restock run…</p>;
  if (result.state === "error") {
    if (result.error instanceof ApiError && result.error.status === 404) {
      return <p role="alert">There is no restock run #{runId}.</p>;
    }
    return <ErrorAlert error={result.error} title="Could not load this run." />;
  }
  return <RunDetails run={result.data} />;
}

function ordersHint(orders: RunPurchaseOrder[]): string {
  if (orders.every((o) => o.status === "DRAFT")) {
    return "Nothing is approved or paid yet. Open each order to edit, reject or approve it.";
  }
  return "Statuses below are live from the database (draft, approved, paid, or cancelled).";
}

function RunDetails({ run }: { run: RestockRunDetail }) {
  const quotes = useMemo(() => extractSupplierQuotes(run.steps), [run.steps]);
  const chosen = useMemo(() => {
    const map = new Map<string, number>();
    for (const po of run.purchase_orders) {
      if (po.status === "CANCELLED") continue;
      for (const line of po.lines) map.set(line.sku, po.supplier.id);
    }
    return map;
  }, [run.purchase_orders]);

  return (
    <div className="stack">
      <section className="card">
        <h2>Restock run #{run.id}</h2>
        <p>
          <span className="hint">You asked:</span> “{run.message}”
        </p>
        <p className="hint">
          Budget {formatCents(run.budget_cents)} · data as of {run.as_of} · {formatDateTime(run.created_at)}
        </p>
        {run.status === "failed" && (
          <div role="alert" className="alert alert-error">
            This run failed{run.error ? ` (${run.error.code})` : ""}. No orders were created.{" "}
            <Link href="/">Try again</Link>.
          </div>
        )}
        {run.warnings.map((w, i) => (
          <div key={i} className="alert alert-warning">
            {w}
          </div>
        ))}
        {run.summary && (
          <div className="ai-summary">
            <span className="ai-label">AI explanation</span>
            <p>{run.summary}</p>
          </div>
        )}
        {run.products.length > 0 && <ProductStockTable products={run.products} />}
      </section>

      <section>
        <h2>Purchase orders</h2>
        {run.purchase_orders.length === 0 ? (
          <p className="card">
            {run.status === "failed"
              ? "No draft orders: the run did not finish."
              : "No draft orders were created. The explanation above says why."}
          </p>
        ) : (
          <div className="stack">
            <p className="hint">{ordersHint(run.purchase_orders)}</p>
            {run.purchase_orders.map((po) => (
              <RunOrderCard key={po.id} order={po} />
            ))}
          </div>
        )}
      </section>

      <section className="card">
        <h2>Supplier options compared</h2>
        <SupplierComparison products={quotes} chosen={chosen} />
      </section>

      <AgentTrace steps={run.steps} />
    </div>
  );
}
