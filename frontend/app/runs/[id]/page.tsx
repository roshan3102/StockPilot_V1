import { Suspense } from "react";

import RunView from "@/components/RunView";
import { parseId } from "@/lib/format";

// A Server Component. With `cacheComponents` on, route params are only known at request time,
// so they are read inside <Suspense> (see node_modules/next/dist/docs, "Dynamic Segments").
export default function RunPage({ params }: PageProps<"/runs/[id]">) {
  return (
    <main className="container">
      <Suspense fallback={<p role="status">Loading restock run…</p>}>
        {params.then(({ id }) => {
          const runId = parseId(id);
          return runId === null ? <p role="alert">“{id}” is not a valid run id.</p> : <RunView runId={runId} />;
        })}
      </Suspense>
    </main>
  );
}
