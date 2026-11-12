import { Suspense } from "react";

import PurchaseOrderView from "@/components/PurchaseOrderView";
import { parseId } from "@/lib/format";

export default function OrderPage({ params }: PageProps<"/orders/[id]">) {
  return (
    <main className="container">
      <Suspense fallback={<p role="status">Loading purchase order…</p>}>
        {params.then(({ id }) => {
          const poId = parseId(id);
          return poId === null ? (
            <p role="alert">“{id}” is not a valid order id.</p>
          ) : (
            <PurchaseOrderView poId={poId} />
          );
        })}
      </Suspense>
    </main>
  );
}
