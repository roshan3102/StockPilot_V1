"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useState } from "react";

import ErrorAlert from "@/components/ErrorAlert";
import {
  ApiError,
  archivePromotion,
  createRestockRun,
  getPromotionEvaluation,
  type PromotionEvaluation,
} from "@/lib/api";
import { formatCents, parseDollarsToCents } from "@/lib/format";
import { formatSize } from "@/lib/units";
import { useLoad } from "@/lib/useLoad";

function cover(days: number | null): string {
  return days === null ? "no recent sales" : `${days} days`;
}

/** One promotion and the system's split of its cases. Every number is the engine's; the AI is
 * only involved when the merchant asks it to draft the order. */
export default function PromotionDetail({ promotionId }: { promotionId: number }) {
  const router = useRouter();
  const load = useCallback(() => getPromotionEvaluation(promotionId), [promotionId]);
  const { result, reload } = useLoad(load);
  const [instructions, setInstructions] = useState("");
  const [budget, setBudget] = useState("");
  const [drafting, setDrafting] = useState(false);
  const [error, setError] = useState<unknown>(null);

  if (result.state === "loading") return <p role="status">Loading promotion…</p>;
  if (result.state === "error") {
    if (result.error instanceof ApiError && result.error.status === 404) {
      return <p role="alert">There is no promotion #{promotionId}.</p>;
    }
    return <ErrorAlert error={result.error} title="Could not load this promotion." />;
  }

  const e: PromotionEvaluation = result.data;
  const p = e.promotion;
  const taken = e.rows.filter((r) => r.cases > 0);

  async function draftWithAgent() {
    const budgetCents = budget.trim() ? parseDollarsToCents(budget) : null;
    if (budget.trim() && budgetCents === null) {
      setError(new Error("Enter the budget as a dollar amount like 2000."));
      return;
    }
    setDrafting(true);
    setError(null);
    const extra = instructions.trim() ? ` Also: ${instructions.trim()}` : "";
    const message =
      `Draft the order for supplier promotion #${p.id} ("${p.title}") using the system's ` +
      `recommended split of cases.${extra}`;
    try {
      const run = await createRestockRun(message, budgetCents);
      router.push(`/runs/${run.id}`);
    } catch (err) {
      setError(err);
      setDrafting(false);
    }
  }

  async function archive() {
    setError(null);
    try {
      await archivePromotion(p.id);
      reload();
    } catch (err) {
      setError(err);
    }
  }

  return (
    <div className="stack">
      <p>
        <Link href="/promotions">← All promotions</Link>
      </p>
      <section className="card">
        <header className="card-header">
          <h2>{p.title}</h2>
          <span className="badge">{p.status}</span>
        </header>
        <p>
          {p.supplier.name}: for every <strong>{p.required_cases} cases</strong> ({p.units_per_case}{" "}
          bottles each) of at least <strong>{p.min_distinct_variants} different flavors</strong>, get{" "}
          <strong>{formatCents(p.rebate_cents)}</strong> back from the supplier
          {p.max_bundles ? ` (at most ${p.max_bundles} per order)` : " (no limit per order)"}.
          {p.valid_until && <> Valid until {p.valid_until}.</>}
          {p.buy_through && <> Stocking up through {p.buy_through}.</>}
        </p>
        <p className="hint">
          Eligible: {p.products.map((pp) => `${pp.variant} ${formatSize(pp.size_ml)}`).join(", ")}
        </p>
        {p.source_text && (
          <p className="hint">
            Supplier’s words: “{p.source_text}”
          </p>
        )}
        {p.unavailable_reason && <div className="alert alert-warning">{p.unavailable_reason}</div>}
      </section>

      <section className="card">
        <h3>Recommended order (from stock and sales)</h3>
        <div className={e.status === "OK" ? "alert alert-info" : "alert alert-error"}>{e.message}</div>
        {e.status === "OK" && (
          <p>
            <strong>
              {e.bundles} {e.bundles === 1 ? "bundle" : "bundles"}
            </strong>{" "}
            covering expected sales through <strong>{e.stock_up_through}</strong> ({e.horizon_days} days):{" "}
            {e.forecast_units} bottles expected
            {e.seasonal ? ", from last year’s sales for the same weeks" : ", at the recent sales rate"}.
          </p>
        )}
        {e.status === "OK" && (
          <>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Flavor</th>
                    <th className="num">Size</th>
                    <th className="num">Cases</th>
                    <th className="num">Bottles</th>
                    <th className="num">Sold/day</th>
                    <th className="num">Expected</th>
                    <th className="num">Last year</th>
                    <th className="num">Cover now</th>
                    <th className="num">Cover after</th>
                    <th className="num">Line total</th>
                  </tr>
                </thead>
                <tbody>
                  {e.rows.map((r) => (
                    <tr key={r.sku} className={r.cases ? "row-chosen" : undefined}>
                      <td>
                        {r.variant} <code>{r.sku}</code>
                      </td>
                      <td className="num">{formatSize(r.size_ml)}</td>
                      <td className="num">{r.cases || "—"}</td>
                      <td className="num">{r.units || "—"}</td>
                      <td className="num">{r.avg_daily_sales}</td>
                      <td className="num">{r.forecast_units}</td>
                      <td className="num" title={r.trend === null ? undefined : `This year runs ${r.trend}x last year`}>
                        {r.last_year_units ?? "—"}
                      </td>
                      <td className="num">{cover(r.days_of_cover_before)}</td>
                      <td className="num">
                        {r.cases ? cover(r.days_of_cover_after) : "—"}
                        {r.over_max_stock_units > 0 && (
                          <span className="hint"> ({r.over_max_stock_units} over max stock)</span>
                        )}
                      </td>
                      <td className="num">{r.cases ? formatCents(r.line_total_cents) : "—"}</td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr>
                    <th colSpan={9}>List total (what you pay now)</th>
                    <td className="num">{formatCents(e.list_total_cents)}</td>
                  </tr>
                  <tr>
                    <th colSpan={9}>
                      Rebate (paid back later)
                      {e.bundles > 1 && `: ${e.bundles} × ${formatCents(e.rebate_per_bundle_cents)}`}
                    </th>
                    <td className="num">−{formatCents(e.rebate_cents)}</td>
                  </tr>
                  <tr>
                    <th colSpan={9}>Net cost</th>
                    <td className="num">
                      <strong>{formatCents(e.net_cents)}</strong>
                    </td>
                  </tr>
                </tfoot>
              </table>
            </div>
            <p className="hint">
              {taken.length} products, {e.distinct_variants} flavors, {e.total_units} bottles.{" "}
              {e.verdict === "EXTRA_STOCK"
                ? `${e.excess_units} bottles go beyond what sales need through ${e.stock_up_through}; weigh that against the rebate.`
                : `Every bottle is within what sales need through ${e.stock_up_through}.`}
            </p>
          </>
        )}
      </section>

      {e.usable && (
        <section className="card">
          <h3>Draft this order</h3>
          <label htmlFor="promo-instructions">Anything to change? (optional)</label>
          <input
            id="promo-instructions"
            placeholder="e.g. no peach"
            value={instructions}
            onChange={(ev) => setInstructions(ev.target.value)}
            disabled={drafting}
          />
          <label htmlFor="promo-budget">Budget in USD (optional)</label>
          <input
            id="promo-budget"
            inputMode="decimal"
            placeholder="Leave empty to use your default budget"
            value={budget}
            onChange={(ev) => setBudget(ev.target.value)}
            disabled={drafting}
          />
          <div className="actions">
            <button type="button" className="btn btn-primary" onClick={draftWithAgent} disabled={drafting}>
              {drafting ? "Drafting…" : "Ask StockPilot to draft this order"}
            </button>
            <button type="button" className="btn" onClick={archive} disabled={drafting}>
              Archive promotion
            </button>
          </div>
          <p className="hint">
            The AI drafts a purchase order that claims this promotion; the server checks the case
            count and flavors. You review and approve it before anything is paid.
          </p>
          {drafting && (
            <p role="status" className="hint">
              The assistant is checking the promotion, stock and prices. This can take up to a minute.
            </p>
          )}
        </section>
      )}

      {error !== null && <ErrorAlert error={error} title="That did not work." />}
    </div>
  );
}
