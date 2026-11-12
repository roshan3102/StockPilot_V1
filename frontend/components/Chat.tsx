"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import ErrorAlert from "@/components/ErrorAlert";
import ProductStockTable from "@/components/ProductStockTable";
import RunOrderCard from "@/components/RunOrderCard";
import {
  ApiError,
  createRestockRun,
  getRestockRun,
  type RestockRun,
  type RunPurchaseOrder,
} from "@/lib/api";
import { MAX_BUDGET_CENTS, formatCents, parseDollarsToCents } from "@/lib/format";

// Mirrors MAX_MESSAGE_CHARS in backend/app/agent/loop.py, for an early, friendly message.
// The backend still enforces it (422 VALIDATION_ERROR) if the two ever drift apart.
export const MAX_MESSAGE_CHARS = 2000;

type FieldErrors = { message?: string; budget?: string };

type Turn =
  | { key: number; message: string; state: "pending" }
  | { key: number; message: string; state: "done"; run: RestockRun }
  | { key: number; message: string; state: "error"; error: unknown };

function validate(message: string, budget: string): { errors: FieldErrors; budgetCents: number | null } {
  const errors: FieldErrors = {};
  const trimmed = message.trim();
  if (!trimmed) errors.message = "Type a question or a restock request.";
  else if (trimmed.length > MAX_MESSAGE_CHARS)
    errors.message = `Keep the message under ${MAX_MESSAGE_CHARS} characters.`;

  let budgetCents: number | null = null;
  if (budget.trim()) {
    budgetCents = parseDollarsToCents(budget);
    if (budgetCents === null) errors.budget = "Enter a dollar amount like 400 or 400.50.";
    else if (budgetCents > MAX_BUDGET_CENTS)
      errors.budget = `The budget cannot exceed ${formatCents(MAX_BUDGET_CENTS)}.`;
  }
  return { errors, budgetCents };
}

/** One chat for questions ("how much Grey Goose 750ml?") and restock requests ("use the Svedka
 * rebate"). Each message is one agent run on the server; there is no memory between messages
 * yet. Numbers shown in tables and order cards come from the database, not the AI's text. */
export default function Chat() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [message, setMessage] = useState("");
  const [budget, setBudget] = useState("");
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const nextKey = useRef(1);
  const turnsRef = useRef(turns);
  turnsRef.current = turns;
  const busy = turns.some((t) => t.state === "pending");

  // The create response freezes each PO as DRAFT. When the merchant comes back from approving,
  // paying or cancelling, re-read those runs so the badges match the database.
  useEffect(() => {
    let cancelled = false;

    async function refreshOrderStatuses() {
      const done = turnsRef.current.filter(
        (t): t is Extract<Turn, { state: "done" }> =>
          t.state === "done" && t.run.purchase_orders.length > 0,
      );
      if (done.length === 0) return;
      const fresh = await Promise.all(
        done.map(async (t) => {
          try {
            const run = await getRestockRun(t.run.id);
            return { key: t.key, purchase_orders: run.purchase_orders };
          } catch {
            return null;
          }
        }),
      );
      if (cancelled) return;
      const byKey = new Map(
        fresh.filter((row): row is { key: number; purchase_orders: RunPurchaseOrder[] } => row !== null)
          .map((row) => [row.key, row.purchase_orders]),
      );
      if (byKey.size === 0) return;
      setTurns((all) =>
        all.map((t) => {
          if (t.state !== "done") return t;
          const purchase_orders = byKey.get(t.key);
          return purchase_orders ? { ...t, run: { ...t.run, purchase_orders } } : t;
        }),
      );
    }

    function onVisible() {
      if (document.visibilityState === "visible") void refreshOrderStatuses();
    }

    window.addEventListener("focus", refreshOrderStatuses);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      window.removeEventListener("focus", refreshOrderStatuses);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);

  async function send() {
    const { errors, budgetCents } = validate(message, budget);
    setFieldErrors(errors);
    if (Object.keys(errors).length > 0 || busy) return;

    const key = nextKey.current++;
    const text = message.trim();
    setTurns((all) => [...all, { key, message: text, state: "pending" }]);
    setMessage("");
    let outcome: Turn;
    try {
      outcome = { key, message: text, state: "done", run: await createRestockRun(text, budgetCents) };
    } catch (error) {
      outcome = { key, message: text, state: "error", error };
    }
    setTurns((all) => all.map((t) => (t.key === key ? outcome : t)));
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void send();
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void send();
    }
  }

  return (
    <section className="card" aria-labelledby="chat-title">
      <h2 id="chat-title">Ask StockPilot</h2>
      <p className="hint">
        Ask about stock, sales or promotions, or ask it to restock. It only ever prepares draft
        orders; you review and approve them.
      </p>

      {turns.length > 0 && (
        <ol className="chat-log" aria-live="polite">
          {turns.map((turn) => (
            <li key={turn.key} className="stack">
              <p className="chat-user">
                <span className="ai-label">You</span> {turn.message}
              </p>
              <TurnReply turn={turn} />
            </li>
          ))}
        </ol>
      )}

      <form onSubmit={handleSubmit} noValidate>
        <label htmlFor="chat-message">Message</label>
        <textarea
          id="chat-message"
          rows={2}
          placeholder="e.g. How much Grey Goose 750ml do we have? · Restock vodka · Use the Svedka rebate"
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={handleKeyDown}
          aria-invalid={Boolean(fieldErrors.message)}
          aria-describedby={fieldErrors.message ? "chat-message-error" : undefined}
        />
        {fieldErrors.message && (
          <p id="chat-message-error" className="field-error">
            {fieldErrors.message}
          </p>
        )}

        <label htmlFor="chat-budget">Budget for orders, in USD (optional)</label>
        <input
          id="chat-budget"
          inputMode="decimal"
          placeholder="Leave empty to use your default budget"
          value={budget}
          onChange={(e) => setBudget(e.target.value)}
          aria-invalid={Boolean(fieldErrors.budget)}
          aria-describedby={fieldErrors.budget ? "chat-budget-error" : undefined}
        />
        {fieldErrors.budget && (
          <p id="chat-budget-error" className="field-error">
            {fieldErrors.budget}
          </p>
        )}
        <p className="hint">
          
        </p>

        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? "Working…" : "Send"}
        </button>
      </form>
    </section>
  );
}

function ordersHint(orders: RunPurchaseOrder[]): string {
  if (orders.every((o) => o.status === "DRAFT")) {
    return "Draft orders: nothing is approved or paid yet. Open each one to edit, reject or approve it.";
  }
  return "Statuses below are live from the database (draft, approved, paid, or cancelled). Open an order for details.";
}

function TurnReply({ turn }: { turn: Turn }) {
  if (turn.state === "pending") {
    return (
      <p role="status" className="hint">
        StockPilot is reading inventory, sales and supplier data. A restock request can take up to
        a minute and a half.
      </p>
    );
  }

  if (turn.state === "error") {
    const failedRunId =
      turn.error instanceof ApiError && typeof turn.error.details.run_id === "number"
        ? turn.error.details.run_id
        : null;
    return (
      <>
        <ErrorAlert error={turn.error} title="StockPilot could not answer." />
        {failedRunId !== null && (
          <p>
            <Link href={`/runs/${failedRunId}`}>See what happened in run #{failedRunId}</Link>.
            You can send the message again to retry.
          </p>
        )}
      </>
    );
  }

  const { run } = turn;
  return (
    <>
      {run.warnings.map((w, i) => (
        <div key={i} className="alert alert-warning">
          {w}
        </div>
      ))}
      {run.summary && (
        <div className="ai-summary">
          <span className="ai-label">StockPilot</span>
          <p>{run.summary}</p>
        </div>
      )}
      {run.products.length > 0 && <ProductStockTable products={run.products} />}
      {run.purchase_orders.length > 0 && (
        <>
          <p className="hint">{ordersHint(run.purchase_orders)}</p>
          {run.purchase_orders.map((po) => (
            <RunOrderCard key={po.id} order={po} />
          ))}
        </>
      )}
      <p className="hint">
        <Link href={`/runs/${run.id}`}>Tool trace and supplier comparison (run #{run.id})</Link>
      </p>
    </>
  );
}
