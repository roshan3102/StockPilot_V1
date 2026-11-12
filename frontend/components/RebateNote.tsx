import Link from "next/link";

import type { POPromotion } from "@/lib/api";
import { formatCents } from "@/lib/format";

/** The rebate an order claims. All amounts come from the server. When `meets_terms` is false
 * the order still stands; it simply does not earn the rebate. */
export default function RebateNote({
  promotion,
  subtotalCents,
}: {
  promotion: POPromotion;
  subtotalCents: number;
}) {
  if (!promotion.meets_terms) {
    return (
      <div className="alert alert-warning">
        <strong>
          Claims <Link href={`/promotions/${promotion.id}`}>{promotion.title}</Link>, but does
          not currently meet the rebate terms.
        </strong>{" "}
        {promotion.terms_message} You pay {formatCents(subtotalCents)} at list price; no rebate
        is counted until the order qualifies.
      </div>
    );
  }

  return (
    <div className="alert alert-info">
      <strong>
        Claims <Link href={`/promotions/${promotion.id}`}>{promotion.title}</Link>.
      </strong>{" "}
      {promotion.terms_message} You pay {formatCents(subtotalCents)} now; the supplier pays back{" "}
      {formatCents(promotion.rebate_cents)} later, for a net cost of{" "}
      <strong>{formatCents(promotion.expected_net_cents)}</strong>.
    </div>
  );
}
