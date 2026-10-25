import type { ApiProblem } from "@/lib/api-client";

export interface FlashMessage {
  text: string;
  ref: string;
}

/** An error line with the request reference, so a screen can be traced to its log line (BUG_TRACEABILITY §2). */
export function Flash({ message, referenceLabel }: { message: FlashMessage | null; referenceLabel: string }) {
  if (!message) return null;
  return (
    <p role="alert" className="issue">
      {message.text}
      {message.ref && (
        <span className="muted">
          {" "}
          ({referenceLabel}: {message.ref})
        </span>
      )}
    </p>
  );
}

export function flashFrom(text: string, p: ApiProblem): FlashMessage {
  return { text, ref: p.requestId };
}
