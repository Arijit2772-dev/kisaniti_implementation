// Browser-side calls to the API through the website's own address (see api/v1/[...path]/route.ts).
/** A full page load, on purpose: after sign-in, sign-out, password or language changes the server pages must be
 *  rendered again with the new cookie, and the client router's cache must not show the old state. */
export function hardNavigate(to: string): void {
  window.location.assign(to);
}

export interface ApiProblem {
  status: number;
  code: string;
  issueCode?: string;
  requestId: string;
  retryAfterS?: number;
}

export type ApiResult<T> = { ok: true; status: number; data: T } | { ok: false; problem: ApiProblem };

export async function api<T>(path: string, init: { method?: string; body?: unknown; csrf?: string; idempotencyKey?: string } = {}): Promise<ApiResult<T>> {
  const headers: Record<string, string> = {};
  if (init.body !== undefined) headers["content-type"] = "application/json";
  if (init.csrf) headers["x-csrf-token"] = init.csrf;
  if (init.idempotencyKey) headers["idempotency-key"] = init.idempotencyKey;
  try {
    const res = await fetch(`/api/v1${path}`, {
      method: init.method ?? "GET",
      headers,
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      credentials: "same-origin",
      cache: "no-store",
    });
    if (res.ok) return { ok: true, status: res.status, data: (res.status === 204 ? null : await res.json()) as T };
    let body: Record<string, unknown> = {};
    try {
      body = (await res.json()) as Record<string, unknown>;
    } catch {
      /* not JSON */
    }
    const issues = Array.isArray(body.issues) ? (body.issues as { code?: string }[]) : [];
    return {
      ok: false,
      problem: {
        status: res.status,
        code: String(body.code ?? "INTERNAL"),
        issueCode: issues[0]?.code,
        requestId: String(body.request_id ?? res.headers.get("x-request-id") ?? ""),
        retryAfterS: typeof body.retry_after_s === "number" ? body.retry_after_s : undefined,
      },
    };
  } catch {
    return { ok: false, problem: { status: 0, code: "NETWORK", requestId: "" } };
  }
}

/** Localised text for an API problem; the backend never translates, it sends codes (CHAT-36). */
export function problemText(
  errors: Record<string, string>,
  p: ApiProblem,
): string {
  if (p.code === "NETWORK" || p.status === 0 || p.status === 503) return errors.network;
  if (p.issueCode === "CSRF_INVALID") return errors.csrf;
  switch (p.code) {
    case "UNAUTHENTICATED": return errors.unauthenticated;
    case "FORBIDDEN": return errors.forbidden;
    case "NOT_FOUND": return errors.notFound;
    case "CONFLICT": return errors.conflict;
    case "VALIDATION_FAILED": return errors.validation;
    default: return errors.internal;
  }
}
