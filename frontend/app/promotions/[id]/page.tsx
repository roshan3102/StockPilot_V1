import { Suspense } from "react";

import PromotionDetail from "@/components/PromotionDetail";
import { parseId } from "@/lib/format";

export default function PromotionPage({ params }: PageProps<"/promotions/[id]">) {
  return (
    <main className="container">
      <Suspense fallback={<p role="status">Loading promotion…</p>}>
        {params.then(({ id }) => {
          const promotionId = parseId(id);
          return promotionId === null ? (
            <p role="alert">“{id}” is not a valid promotion id.</p>
          ) : (
            <PromotionDetail promotionId={promotionId} />
          );
        })}
      </Suspense>
    </main>
  );
}
