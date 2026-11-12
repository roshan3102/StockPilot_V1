// Typed wrapper around fetch for talking to the FastAPI backend.
// Every backend call goes through this file, so the base URL and error handling live in one place.
//
// The types below mirror the Pydantic response models in backend/app/schemas.py. Money is
// integer cents, exactly as the backend sends it: the frontend never computes a total.

// NEXT_PUBLIC_* variables are inlined into the browser bundle at build time, so this must be
// non-secret. Set it in frontend/.env.local; the fallback matches the backend's default port.
export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export type HealthResponse = {
  status: string;
  env: string;
};

export async function getHealth(): Promise<HealthResponse> {
  const response = await fetch(`${API_BASE_URL}/health`);

  // fetch() only rejects on network failure. An HTTP 500 still "succeeds", so check .ok ourselves.
  if (!response.ok) {
    throw new Error(`Health check failed with HTTP ${response.status}`);
  }

  return (await response.json()) as HealthResponse;
}

// --- shared shapes -------------------------------------------------------------------------

export type POStatus =
  | "DRAFT"
  | "APPROVED"
  | "PAYMENT_PENDING"
  | "PAID"
  | "PAYMENT_FAILED"
  | "CANCELLED";

export type SupplierRef = { id: number; name: string };

export type Violation = { code: string; sku: string | null; message: string };

/** The promotion an order claims. The order (and PayPal) is charged the list subtotal; the
 * supplier pays `rebate_cents` back later when `meets_terms` is true. Not meeting the terms
 * does not block editing or approving. */
export type POPromotion = {
  id: number;
  title: string;
  /** Earned rebate for the current lines; 0 if terms are not met. */
  rebate_cents: number;
  rebate_per_bundle_cents: number;
  expected_net_cents: number;
  meets_terms: boolean;
  terms_message: string;
  required_cases: number;
  units_per_case: number;
  min_distinct_variants: number;
  max_bundles: number | null;
  eligible: { sku: string; name: string; variant: string; size_ml: number | null }[];
};

// --- /restock-runs -------------------------------------------------------------------------

export type RunEvidence = {
  on_hand: number;
  avg_daily_sales: number;
  days_of_cover: number | null;
};

export type RunLine = {
  sku: string;
  name: string;
  quantity: number;
  pack_size: number;
  unit_price_cents: number;
  line_total_cents: number;
  reason: string | null;
  evidence: RunEvidence;
};

export type RunPurchaseOrder = {
  id: number;
  supplier: SupplierRef;
  status: POStatus;
  version: number;
  subtotal_cents: number;
  currency: "USD";
  rationale: string | null;
  promotion: POPromotion | null;
  lines: RunLine[];
};

export type AgentStep = {
  seq: number;
  kind: "assistant" | "tool_call" | "tool_result";
  name: string | null;
  payload: Record<string, unknown>;
};

export type RestockRun = {
  id: number;
  status: "running" | "completed" | "failed";
  message: string;
  budget_cents: number;
  currency: "USD";
  model: string;
  created_at: string;
  finished_at: string | null;
  as_of: string;
  summary: string | null;
  error: { code: string; message: string } | null;
  warnings: string[];
  step_count: number;
  purchase_orders: RunPurchaseOrder[];
  /** Products the assistant looked up, with stock as the database reported it. */
  products: ProductStock[];
};

export type RestockRunDetail = RestockRun & { steps: AgentStep[] };

// --- /purchase-orders ----------------------------------------------------------------------

export type Payment = {
  id: number;
  paypal_order_id: string;
  status: "CREATED" | "PENDING" | "CAPTURED" | "FAILED";
  amount_cents: number;
  currency: "USD";
  paypal_capture_id: string | null;
  verified_at: string | null;
  error: string | null;
  created_at: string;
};

export type PurchaseOrderLine = {
  sku: string;
  name: string;
  quantity: number;
  pack_size: number;
  unit_price_cents: number;
  line_total_cents: number;
  reason: string | null;
};

export type PurchaseOrderEvent = {
  id: number;
  event: string;
  detail: Record<string, unknown>;
  at: string;
};

export type PurchaseOrder = {
  id: number;
  status: POStatus;
  version: number;
  supplier: SupplierRef;
  currency: "USD";
  subtotal_cents: number;
  budget_cents: number | null;
  run_id: number | null;
  rationale: string | null;
  promotion: POPromotion | null;
  created_at: string;
  approved_total_cents: number | null;
  approved_at: string | null;
  approved_snapshot_sha256: string | null;
  lines: PurchaseOrderLine[];
  payments: Payment[];
  events: PurchaseOrderEvent[];
};

export type PurchaseOrderSummary = {
  id: number;
  status: POStatus;
  supplier: SupplierRef;
  currency: "USD";
  subtotal_cents: number;
  line_count: number;
  run_id: number | null;
  created_at: string;
  approved_at: string | null;
  latest_payment: Payment | null;
};

// A requested line: SKU and quantity only. There is deliberately no price field.
export type LineInput = { sku: string; quantity: number; reason?: string | null };

export type CreatePayPalOrderResponse = {
  paypal_order_id: string;
  amount_cents: number;
  currency: "USD";
  payment_status: string;
  approve_url: string | null;
  resumed: boolean;
};

export type CaptureResponse = {
  outcome: "PAID" | "PENDING";
  po_status: POStatus;
  payment: Payment;
};

// --- /suppliers and /promotions ------------------------------------------------------------

export type Supplier = { id: number; name: string; min_order_cents: number };

export type CatalogOffer = {
  sku: string;
  name: string;
  brand: string | null;
  variant: string | null;
  size_ml: number | null;
  unit_price_cents: number;
  pack_size: number;
};

/** The AI's reading of a promotion's text, matched to the catalog. Not saved yet. */
export type PromotionDraft = {
  title: string;
  kind: string;
  supplier_id: number | null;
  supplier_name_as_written: string | null;
  brand: string | null;
  required_cases: number | null;
  units_per_case: number;
  rebate_cents: number | null;
  min_distinct_variants: number | null;
  max_bundles: number | null;
  sizes_ml: number[];
  eligible_skus: string[];
  valid_from: string | null;
  valid_until: string | null;
  ambiguities: string[];
  warnings: string[];
  source_text: string;
};

export type PromotionInput = {
  supplier_id: number;
  title: string;
  required_cases: number;
  units_per_case: number;
  rebate_cents: number;
  min_distinct_variants: number;
  eligible_skus: string[];
  valid_from: string | null;
  valid_until: string | null;
  source_text: string | null;
  /** Most rebates one order can earn; null = no limit. */
  max_bundles: number | null;
  /** Stock up for demand until this date (YYYY-MM-DD). */
  buy_through: string | null;
};

export type Promotion = {
  id: number;
  supplier: SupplierRef;
  title: string;
  kind: string;
  status: "ACTIVE" | "ARCHIVED";
  required_cases: number;
  units_per_case: number;
  /** Per bundle. */
  rebate_cents: number;
  min_distinct_variants: number;
  max_bundles: number | null;
  valid_from: string | null;
  valid_until: string | null;
  buy_through: string | null;
  source_text: string | null;
  created_at: string;
  unavailable_reason: string | null;
  products: { sku: string; name: string; variant: string; size_ml: number | null }[];
};

export type AllocationRow = {
  sku: string;
  name: string;
  variant: string;
  size_ml: number | null;
  cases: number;
  units: number;
  unit_price_cents: number;
  line_total_cents: number;
  avg_daily_sales: number;
  /** Expected sales until the stock-up date. */
  forecast_units: number;
  /** SEASONAL: last year's same weeks x this year's trend; RECENT: the recent rate. */
  forecast_method: "SEASONAL" | "RECENT";
  last_year_units: number | null;
  trend: number | null;
  shortfall_units: number;
  excess_units: number;
  over_max_stock_units: number;
  days_of_cover_before: number | null;
  days_of_cover_after: number | null;
};

export type PromotionEvaluation = {
  promotion: Promotion;
  usable: boolean;
  status: "OK" | "INFEASIBLE";
  rows: AllocationRow[];
  currency: "USD";
  list_total_cents: number;
  bundles: number;
  rebate_per_bundle_cents: number;
  rebate_cents: number;
  net_cents: number;
  total_units: number;
  distinct_variants: number;
  forecast_units: number;
  need_units: number;
  excess_units: number;
  horizon_days: number;
  stock_up_through: string;
  seasonal: boolean;
  verdict: "ALL_NEEDED" | "EXTRA_STOCK" | null;
  message: string;
};

// --- product stock (lookup results shown beside the AI's answer) ---------------------------

export type ProductStock = {
  sku: string;
  name: string;
  size_ml: number | null;
  on_hand: number;
  on_order: number;
  avg_daily_sales: number;
  days_of_cover: number | null;
};

// --- errors --------------------------------------------------------------------------------

/** Any failed API call. `code` is the backend's machine-readable error code (see the
 * docstrings in backend/app/api/*.py), so components can react to specific cases. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }

  /** The purchasing-rule violations of a 422 ORDER_REJECTED, or an empty list. */
  get violations(): Violation[] {
    const value = this.details.violations;
    return Array.isArray(value) ? (value as Violation[]) : [];
  }
}

export const NETWORK_ERROR = "NETWORK_ERROR";

type ErrorEnvelope = { error?: { code?: unknown; message?: unknown; details?: unknown } };

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      headers: init.body ? { "Content-Type": "application/json" } : undefined,
    });
  } catch {
    // fetch rejects only when no HTTP response arrived at all (server down, CORS, offline).
    throw new ApiError(0, NETWORK_ERROR, "Could not reach the StockPilot API. Is the backend running?");
  }

  // Some bodies may not be JSON (e.g. a proxy's HTML error page); treat that as "no body".
  const body: unknown = await response.json().catch(() => null);

  if (!response.ok) {
    const err = (body as ErrorEnvelope | null)?.error;
    throw new ApiError(
      response.status,
      typeof err?.code === "string" ? err.code : `HTTP_${response.status}`,
      typeof err?.message === "string" ? err.message : `Request failed with HTTP ${response.status}`,
      err?.details && typeof err.details === "object" ? (err.details as Record<string, unknown>) : {},
    );
  }
  return body as T;
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

// --- endpoints -----------------------------------------------------------------------------

export function createRestockRun(message: string, budgetCents: number | null): Promise<RestockRun> {
  // Omit budget_cents entirely to use the merchant's default budget.
  const body = budgetCents === null ? { message } : { message, budget_cents: budgetCents };
  return post<RestockRun>("/restock-runs", body);
}

export function getRestockRun(runId: number): Promise<RestockRunDetail> {
  return request<RestockRunDetail>(`/restock-runs/${runId}`);
}

export async function listPurchaseOrders(): Promise<PurchaseOrderSummary[]> {
  const data = await request<{ items: PurchaseOrderSummary[] }>("/purchase-orders");
  return data.items;
}

export function getPurchaseOrder(poId: number): Promise<PurchaseOrder> {
  return request<PurchaseOrder>(`/purchase-orders/${poId}`);
}

/** Replace a draft's lines. The server re-prices them and returns the new version. */
export function editPurchaseOrder(
  poId: number,
  expectedVersion: number,
  lines: LineInput[],
): Promise<PurchaseOrder> {
  return request<PurchaseOrder>(`/purchase-orders/${poId}`, {
    method: "PUT",
    body: JSON.stringify({ expected_version: expectedVersion, lines }),
  });
}

/** `approvedTotalCents` is the total the merchant SAW (from the server); the backend refuses
 * if it no longer matches, so the merchant can never approve a number they did not see. */
export function approvePurchaseOrder(
  poId: number,
  approvedTotalCents: number,
  expectedVersion: number,
): Promise<PurchaseOrder> {
  return post<PurchaseOrder>(`/purchase-orders/${poId}/approve`, {
    approved_total_cents: approvedTotalCents,
    expected_version: expectedVersion,
  });
}

export function cancelPurchaseOrder(poId: number, reason: string | null): Promise<PurchaseOrder> {
  return post<PurchaseOrder>(`/purchase-orders/${poId}/cancel`, reason ? { reason } : undefined);
}

/** No body: the server takes the amount from the approved snapshot. */
export function createPayPalOrder(poId: number): Promise<CreatePayPalOrderResponse> {
  return post<CreatePayPalOrderResponse>(`/purchase-orders/${poId}/paypal/orders`);
}

export async function listSuppliers(): Promise<Supplier[]> {
  return (await request<{ items: Supplier[] }>("/suppliers")).items;
}

export async function listSupplierOffers(supplierId: number): Promise<CatalogOffer[]> {
  return (await request<{ items: CatalogOffer[] }>(`/suppliers/${supplierId}/offers`)).items;
}

/** The AI reads the text; nothing is saved until `createPromotion`. */
export function parsePromotion(text: string): Promise<PromotionDraft> {
  return post<PromotionDraft>("/promotions/parse", { text });
}

export function createPromotion(input: PromotionInput): Promise<Promotion> {
  return post<Promotion>("/promotions", input);
}

export async function listPromotions(): Promise<Promotion[]> {
  return (await request<{ items: Promotion[] }>("/promotions")).items;
}

export function getPromotionEvaluation(promotionId: number): Promise<PromotionEvaluation> {
  return request<PromotionEvaluation>(`/promotions/${promotionId}/evaluation`);
}

export function archivePromotion(promotionId: number): Promise<Promotion> {
  return post<Promotion>(`/promotions/${promotionId}/archive`);
}

/** Ask the server to verify and capture. Idempotent: also how to re-check a pending payment. */
export function capturePayPalOrder(poId: number, paypalOrderId: string): Promise<CaptureResponse> {
  return post<CaptureResponse>(`/purchase-orders/${poId}/paypal/capture`, {
    paypal_order_id: paypalOrderId,
  });
}
