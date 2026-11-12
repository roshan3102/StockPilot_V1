import type { AgentStep } from "@/lib/api";

function label(step: AgentStep): string {
  if (step.kind === "assistant") return "Assistant message";
  if (step.kind === "tool_call") return `Called ${step.name}`;
  return step.payload.is_error ? `${step.name} returned an error` : `${step.name} result`;
}

/** The full tool trace, collapsed by default: every call the AI made and what came back. */
export default function AgentTrace({ steps }: { steps: AgentStep[] }) {
  return (
    <details className="card">
      <summary>Tool trace ({steps.length} steps)</summary>
      <ol className="trace">
        {steps.map((step) => (
          <li key={step.seq} className={step.payload.is_error ? "trace-error" : undefined}>
            <details>
              <summary>
                #{step.seq} {label(step)}
              </summary>
              <pre>{JSON.stringify(step.payload, null, 2)}</pre>
            </details>
          </li>
        ))}
      </ol>
    </details>
  );
}
