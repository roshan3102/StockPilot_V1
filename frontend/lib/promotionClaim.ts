import type { POPromotion } from "@/lib/api";
import { formatCents } from "@/lib/format";

/** One line the draft editor is about to send: units = cases × pack size. */
export type ClaimLine = { sku: string; quantity: number };

export type PromotionClaimView = {
  meetsTerms: boolean;
  rebateCents: number;
  message: string;
};

/** Mirror of backend `promotion_claim` for live feedback while editing. The server re-checks
 * on save; this only drives the advisory banner. */
export function promotionClaim(promo: POPromotion, lines: ClaimLine[]): PromotionClaimView {
  const eligible = new Map(promo.eligible.map((p) => [p.sku, p.variant]));
  const promoLines = lines.filter((l) => eligible.has(l.sku) && l.quantity > 0);
  const problems: string[] = [];

  for (const line of promoLines) {
    if (line.quantity % promo.units_per_case !== 0) {
      problems.push(
        `${line.sku}: ${line.quantity} units is not a whole number of ${promo.units_per_case}-bottle cases`,
      );
    }
  }

  const units = promoLines.reduce((sum, l) => sum + l.quantity, 0);
  const requiredUnits = promo.required_cases * promo.units_per_case;
  const bundles = Math.floor(units / requiredUnits);
  const leftover = units % requiredUnits;
  const tooMany = promo.max_bundles !== null && bundles > promo.max_bundles;
  if (bundles === 0 || leftover !== 0 || tooMany) {
    const limit = promo.max_bundles === null ? "" : `, at most ${promo.max_bundles}`;
    problems.push(
      `The order has ${units} units (${units / promo.units_per_case} cases) of eligible ` +
        `products; the promotion needs whole bundles of ${promo.required_cases} cases ` +
        `(${requiredUnits} units)${limit}`,
    );
  }

  const variants = new Set(promoLines.map((l) => eligible.get(l.sku)!));
  if (variants.size < promo.min_distinct_variants) {
    problems.push(
      `The order covers ${variants.size} different flavor(s); the promotion needs at ` +
        `least ${promo.min_distinct_variants}`,
    );
  }

  if (problems.length > 0) {
    return {
      meetsTerms: false,
      rebateCents: 0,
      message: `Does not meet rebate requirements: ${problems.join("; ")}.`,
    };
  }

  const capped = promo.max_bundles === null ? bundles : Math.min(bundles, promo.max_bundles);
  const rebate = capped * promo.rebate_per_bundle_cents;
  const plural = capped === 1 ? "" : "s";
  return {
    meetsTerms: true,
    rebateCents: rebate,
    message:
      `Meets rebate requirements: ${capped} bundle${plural}, ${formatCents(rebate)} back ` +
      `from the supplier.`,
  };
}
