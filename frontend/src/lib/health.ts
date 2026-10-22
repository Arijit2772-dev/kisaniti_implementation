// Reads the backend's readiness/v1 body (contracts/shared/v1/readiness.schema.json).
import { connection } from "next/server";

export type ReadinessStatus = "READY" | "READY_WITH_WARNINGS" | "BLOCKED";

export interface Issue {
  code: string;
  path?: string;
  message: string;
  severity?: "ERROR" | "WARNING";
}

export interface Readiness {
  contract: "readiness/v1";
  purpose: string;
  status: ReadinessStatus;
  components?: Record<string, ReadinessStatus>;
  issues: Issue[];
  policy_version: string;
  evaluated_at: string;
}

export interface Meta {
  api_version: string;
  core_version: string;
  contracts: string[];
  mode: "RESEARCH" | "DEMO" | "LIVE";
}

export type ApiResult<T> = { ok: true; data: T } | { ok: false; reason: string };

const API_URL = process.env.KISANITI_API_URL ?? "http://localhost:8000";

async function getJson<T>(path: string): Promise<ApiResult<T>> {
  await connection(); // health must be read per request, never prerendered
  try {
    const res = await fetch(`${API_URL}${path}`, { cache: "no-store", signal: AbortSignal.timeout(3000) });
    const body = (await res.json()) as T;
    return { ok: true, data: body };
  } catch (e) {
    return { ok: false, reason: e instanceof Error ? e.message : String(e) };
  }
}

export const getReadiness = () => getJson<Readiness>("/health/ready");
export const getMeta = () => getJson<Meta>("/api/v1/meta");
