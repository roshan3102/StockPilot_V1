import type { POStatus } from "@/lib/api";

const LABELS: Record<POStatus, string> = {
  DRAFT: "Draft",
  APPROVED: "Approved",
  PAYMENT_PENDING: "Awaiting payment",
  PAID: "Paid (verified)",
  PAYMENT_FAILED: "Payment failed",
  CANCELLED: "Rejected / cancelled",
};

export default function StatusBadge({ status }: { status: POStatus }) {
  return <span className={`badge badge-${status.toLowerCase()}`}>{LABELS[status]}</span>;
}
