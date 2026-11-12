// Reads the supplier comparison out of a run's tool trace.
//
// The agent's `get_supplier_quotes` tool returns every supplier's price, case size and minimum
// for the SKUs it asked about. Those numbers were read from the database by the backend (see
// backend/app/agent/tools.py), not written by the LLM, so they are safe to show as evidence.
// The trace is stored as loosely-typed JSON, so this code checks shapes instead of trusting them.

import type { AgentStep } from "@/lib/api";

export type SupplierOffer = {
  supplier_id: number;
  supplier_name: string;
  unit_price_cents: number;
  pack_size: number;
  supplier_min_order_cents: number;
  lead_time_days: number;
  quote_for_shortfall?: { quantity: number; line_total_cents: number; capped_by_max_stock: boolean };
  unusable_because?: string;
};

export type ProductQuotes = {
  sku: string;
  name: string;
  shortfall_units: number;
  offers: SupplierOffer[];
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isProductQuotes(value: unknown): value is ProductQuotes {
  return (
    isRecord(value) &&
    typeof value.sku === "string" &&
    typeof value.name === "string" &&
    Array.isArray(value.offers)
  );
}

/** One entry per SKU the agent compared. If it compared a SKU more than once, the latest wins. */
export function extractSupplierQuotes(steps: AgentStep[]): ProductQuotes[] {
  const bySku = new Map<string, ProductQuotes>();
  for (const step of steps) {
    if (step.kind !== "tool_result" || step.name !== "get_supplier_quotes") continue;
    if (step.payload.is_error) continue;
    const result = step.payload.result;
    if (!isRecord(result) || !Array.isArray(result.products)) continue;
    for (const product of result.products) {
      if (isProductQuotes(product)) bySku.set(product.sku, product);
    }
  }
  return [...bySku.values()];
}
