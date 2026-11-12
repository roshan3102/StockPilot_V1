"use client";

import { useEffect, useRef, useState } from "react";

import { createPayPalOrder } from "@/lib/api";
import { loadPayPalSdk } from "@/lib/paypal";

type Props = {
  poId: number;
  /** PayPal says the buyer approved in the popup. This is only a hint: the caller must ask the
   * backend to verify and capture before showing anything as paid. */
  onApproved: (paypalOrderId: string) => Promise<void>;
  onCancelled: () => void;
  onError: (error: unknown) => void;
};

/** Renders PayPal's Smart Buttons. The buttons call OUR backend to create the PayPal order
 * (the amount comes from the approved order on the server), then hand back the order id. */
export default function PayPalButtons({ poId, onApproved, onCancelled, onError }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [sdkState, setSdkState] = useState<"loading" | "ready" | "error">("loading");
  const [sdkError, setSdkError] = useState<string | null>(null);

  // Keep the latest callbacks in a ref so a parent re-render does not tear down the buttons.
  const callbacks = useRef({ onApproved, onCancelled, onError });
  useEffect(() => {
    callbacks.current = { onApproved, onCancelled, onError };
  });

  useEffect(() => {
    let cancelled = false;
    let buttons: { close: () => Promise<void> } | null = null;
    // The error our backend raised inside createOrder. PayPal's SDK wraps errors thrown in its
    // callbacks into a generic one, so we keep the original to show its real message.
    let createError: unknown = null;

    loadPayPalSdk()
      .then((paypal) => {
        if (cancelled || !containerRef.current) return;
        const instance = paypal.Buttons({
          style: { layout: "vertical", label: "pay" },
          createOrder: async () => {
            createError = null;
            try {
              return (await createPayPalOrder(poId)).paypal_order_id;
            } catch (err) {
              createError = err;
              throw err;
            }
          },
          onApprove: (data) => callbacks.current.onApproved(data.orderID),
          onCancel: () => callbacks.current.onCancelled(),
          onError: (err) => callbacks.current.onError(createError ?? err),
        });
        buttons = instance;
        return instance.render(containerRef.current).then(() => {
          if (!cancelled) setSdkState("ready");
        });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setSdkState("error");
        setSdkError(err instanceof Error ? err.message : "Could not load PayPal checkout.");
      });

    return () => {
      cancelled = true;
      buttons?.close().catch(() => {}); // the SDK rejects if it was already closed
    };
  }, [poId]);

  return (
    <div>
      {sdkState === "loading" && <p role="status">Loading PayPal checkout…</p>}
      {sdkState === "error" && (
        <p role="alert" className="alert alert-error">
          {sdkError}
        </p>
      )}
      <div ref={containerRef} data-testid="paypal-buttons" />
    </div>
  );
}
