// Loads the PayPal JS SDK (https://developer.paypal.com/sdk/js/) with a plain <script> tag.
//
// Only the CLIENT ID is used here. It is public by design (it identifies the app to PayPal);
// the client SECRET lives only on the backend. A Sandbox client id makes the SDK use Sandbox.

export const PAYPAL_CLIENT_ID = process.env.NEXT_PUBLIC_PAYPAL_CLIENT_ID ?? "";

// The small part of the SDK's API that we use.
export type PayPalButtonsOptions = {
  createOrder: () => Promise<string>;
  onApprove: (data: { orderID: string }) => Promise<void>;
  onCancel?: (data: { orderID?: string }) => void;
  onError?: (error: unknown) => void;
  style?: Record<string, string>;
};

export type PayPalButtonsInstance = {
  render: (container: HTMLElement) => Promise<void>;
  close: () => Promise<void>;
  isEligible: () => boolean;
};

export type PayPalNamespace = {
  Buttons: (options: PayPalButtonsOptions) => PayPalButtonsInstance;
};

declare global {
  interface Window {
    paypal?: PayPalNamespace;
  }
}

// One shared promise: every component that needs the SDK waits on the same <script> load.
let sdkPromise: Promise<PayPalNamespace> | null = null;

export function loadPayPalSdk(clientId: string = PAYPAL_CLIENT_ID): Promise<PayPalNamespace> {
  if (!clientId) {
    return Promise.reject(
      new Error("NEXT_PUBLIC_PAYPAL_CLIENT_ID is not set. Add it to frontend/.env.local and restart."),
    );
  }
  if (window.paypal) return Promise.resolve(window.paypal);
  if (sdkPromise) return sdkPromise;

  sdkPromise = new Promise<PayPalNamespace>((resolve, reject) => {
    // Demo: PayPal wallet only — no guest card checkout or Pay Later.
    const params = new URLSearchParams({
      "client-id": clientId,
      currency: "USD",
      intent: "capture",
      "disable-funding": "card,credit,paylater",
    });
    const script = document.createElement("script");
    script.src = `https://www.paypal.com/sdk/js?${params}`;
    script.async = true;
    script.onload = () =>
      window.paypal ? resolve(window.paypal) : reject(new Error("PayPal SDK loaded without `paypal`"));
    script.onerror = () => {
      sdkPromise = null; // allow a retry after e.g. a network blip
      script.remove();
      reject(new Error("Could not load PayPal checkout. Check your connection and try again."));
    };
    document.head.appendChild(script);
  });
  return sdkPromise;
}
