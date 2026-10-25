import "server-only";
import { cookies } from "next/headers";

// Server-side view of the signed-in user (PrincipalV1, contract access/v1). The browser's session cookie is passed to
// the API as it is; the API is the only place that decides who is signed in.
export type Role = "FPO_ADMIN" | "STAFF" | "VIEWER" | "PLATFORM_OPERATOR";

export interface Principal {
  contract: "access.principal/v1";
  user_id: string;
  display_name: string;
  fpo_id: string | null;
  fpo_name: string | null;
  role: Role;
  capabilities: string[];
  language: "pa" | "en";
  must_change_password: boolean;
  csrf_token: string;
  session_expires_at: string;
}

const API_URL = process.env.KISANITI_API_URL ?? "http://localhost:8000";
export const SESSION_COOKIE = "kn_session";

export async function getPrincipal(): Promise<Principal | null> {
  const token = (await cookies()).get(SESSION_COOKIE)?.value;
  if (!token) return null;
  try {
    const res = await fetch(`${API_URL}/api/v1/auth/me`, {
      headers: { cookie: `${SESSION_COOKIE}=${token}` },
      cache: "no-store",
      signal: AbortSignal.timeout(4000),
    });
    if (res.status === 401) return null;
    if (!res.ok) return null;
    return (await res.json()) as Principal;
  } catch {
    return null;
  }
}

/** Server-side GET with the browser's session cookie, for data a page needs at first render. Null on any failure. */
export async function serverGet<T>(path: string): Promise<T | null> {
  const token = (await cookies()).get(SESSION_COOKIE)?.value;
  if (!token) return null;
  try {
    const res = await fetch(`${API_URL}/api/v1${path}`, {
      headers: { cookie: `${SESSION_COOKIE}=${token}` },
      cache: "no-store",
      signal: AbortSignal.timeout(4000),
    });
    return res.ok ? ((await res.json()) as T) : null;
  } catch {
    return null;
  }
}
