import { ApiError } from "@/lib/api";

/** Shows any caught error. For an ApiError it also lists purchasing-rule violations, which
 * come from the backend's validator (the frontend does not re-implement those rules). */
export default function ErrorAlert({ error, title }: { error: unknown; title?: string }) {
  const message = error instanceof Error ? error.message : "Something went wrong.";
  const violations = error instanceof ApiError ? error.violations : [];
  // With violations, the top-level message is just those messages joined, so list them instead.
  return (
    <div role="alert" className="alert alert-error">
      {title && <strong>{title} </strong>}
      {violations.length > 0 ? (
        <ul>
          {violations.map((v, i) => (
            <li key={i}>{v.message}</li>
          ))}
        </ul>
      ) : (
        message
      )}
    </div>
  );
}

/** A short, human explanation for error codes where the merchant needs to do something. */
export function describeError(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  switch (error.code) {
    case "STALE_DRAFT":
      return "This draft changed since you loaded it. The latest version is shown; review it again.";
    case "TOTAL_MISMATCH":
    case "PRICES_CHANGED":
      return "The total changed on the server. The latest numbers are shown; review them before approving.";
    case "PO_NOT_APPROVABLE":
    case "PO_NOT_EDITABLE":
    case "PO_NOT_CANCELLABLE":
      return "This order is no longer a draft. The current status is shown.";
    default:
      return null;
  }
}
