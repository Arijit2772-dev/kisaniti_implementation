// Runtime proxy from the website to the API, so the session cookie belongs to the website's own address and the API
// address is read when the container starts (KISANITI_API_URL), not frozen at build time.
// Only /api/v1/* is forwarded. The browser's own cookie, CSRF header and Origin go through unchanged; the API decides.
import { randomUUID } from "node:crypto";
import type { NextRequest } from "next/server";

const API_URL = process.env.KISANITI_API_URL ?? "http://localhost:8000";
const FORWARD_REQUEST = ["cookie", "content-type", "x-csrf-token", "origin", "accept-language"];
const SKIP_RESPONSE = new Set(["content-encoding", "content-length", "transfer-encoding", "connection", "set-cookie"]);

async function forward(request: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  const { path } = await params;
  const target = `${API_URL}/api/v1/${path.map(encodeURIComponent).join("/")}${request.nextUrl.search}`;
  const headers = new Headers();
  for (const name of FORWARD_REQUEST) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  headers.set("x-request-id", request.headers.get("x-request-id") ?? randomUUID().replaceAll("-", ""));
  const hasBody = !["GET", "HEAD"].includes(request.method);
  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body: hasBody ? await request.arrayBuffer() : undefined,
      redirect: "manual",
      cache: "no-store",
      signal: AbortSignal.timeout(15000),
    });
    const out = new Headers();
    upstream.headers.forEach((value, name) => {
      if (!SKIP_RESPONSE.has(name)) out.set(name, value);
    });
    for (const cookie of upstream.headers.getSetCookie()) out.append("set-cookie", cookie);
    return new Response(upstream.status === 204 ? null : await upstream.arrayBuffer(), { status: upstream.status, headers: out });
  } catch (e) {
    const requestId = headers.get("x-request-id") ?? "";
    const body = {
      type: "https://kisaniti.invalid/errors/DEPENDENCY_UNAVAILABLE",
      title: "Dependency unavailable",
      status: 503,
      code: "DEPENDENCY_UNAVAILABLE",
      detail: `the API did not answer (${e instanceof Error ? e.name : "error"})`,
      instance: `/api/v1/${path.join("/")}`,
      request_id: requestId,
    };
    return new Response(JSON.stringify(body), {
      status: 503,
      headers: { "content-type": "application/problem+json", "x-request-id": requestId },
    });
  }
}

export { forward as GET, forward as POST, forward as PUT, forward as PATCH, forward as DELETE };
