"use client";

// "use client" marks this as a Client Component: it runs in the browser, which is required
// for useState/useEffect. Components without it are Server Components (rendered on the server).

import { useEffect, useState } from "react";

import { getHealth, type HealthResponse } from "@/lib/api";

// A discriminated union: `state` tells TypeScript which other fields exist.
// This makes impossible combinations (e.g. loading AND an error message) unrepresentable.
type HealthState =
  | { state: "loading" }
  | { state: "ok"; data: HealthResponse }
  | { state: "error"; message: string };

export default function HealthStatus() {
  const [health, setHealth] = useState<HealthState>({ state: "loading" });

  useEffect(() => {
    // `cancelled` stops us updating state if the component unmounts before the request finishes.
    let cancelled = false;

    getHealth()
      .then((data) => {
        if (!cancelled) setHealth({ state: "ok", data });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          const message = error instanceof Error ? error.message : "Unknown error";
          setHealth({ state: "error", message });
        }
      });

    // The function returned from useEffect is cleanup, run on unmount.
    return () => {
      cancelled = true;
    };
  }, []); // Empty dependency array: run once after the first render.

  if (health.state === "loading") {
    return <p>Checking API…</p>;
  }
  if (health.state === "error") {
    return <p role="alert">API unreachable: {health.message}</p>;
  }
  return (
    <p>
      API status: <strong>{health.data.status}</strong> (env: {health.data.env})
    </p>
  );
}
