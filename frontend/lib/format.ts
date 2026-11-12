// Display and input helpers. Nothing here computes a price, a total or a budget rule: the
// backend does that. These only turn the backend's integer cents into text and back.

const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

/** 15360 -> "$153.60". Display only. */
export function formatCents(cents: number): string {
  return usd.format(cents / 100);
}

/** 24 units in packs of 12 -> "2 cases of 12". Quantities are always whole cases (the backend
 * enforces it), so the division is exact. */
export function formatCases(quantity: number, packSize: number): string {
  const cases = quantity / packSize;
  return `${cases} ${cases === 1 ? "case" : "cases"} of ${packSize}`;
}

/** The backend's sanity ceiling for a budget ($1,000,000), mirrored for a friendlier message. */
export const MAX_BUDGET_CENTS = 100_000_000;

/** Parse what a merchant typed as a dollar amount into integer cents, or null if it is not a
 * plain amount like "400", "400.5" or "1,250.00". Done on the string, never via floating
 * point: 0.1 + 0.2 !== 0.3 in JavaScript, and money must be exact. */
export function parseDollarsToCents(input: string): number | null {
  const cleaned = input.trim().replace(/^\$/, "").replace(/,/g, "");
  const match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(cleaned);
  if (!match) return null;
  const [, whole, fraction = ""] = match;
  return Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
}

export function formatDateTime(iso: string): string {
  // The backend stores UTC without a timezone suffix; say so, or the browser assumes local time.
  const normalised = /[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`;
  const date = new Date(normalised);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

/** A route segment like "12" -> 12; anything else (e.g. "abc", "0", "1.5") -> null. */
export function parseId(segment: string): number | null {
  return /^[1-9]\d{0,15}$/.test(segment) ? Number(segment) : null;
}
