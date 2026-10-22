import { Suspense } from "react";
import { getMeta, getReadiness, type ReadinessStatus } from "@/lib/health";

const LABEL: Record<ReadinessStatus, string> = {
  READY: "Ready",
  READY_WITH_WARNINGS: "Ready with warnings",
  BLOCKED: "Blocked",
};

async function Status() {
  const [ready, meta] = await Promise.all([getReadiness(), getMeta()]);
  if (!ready.ok) {
    return (
      <section className="card blocked" data-testid="health">
        <h2>System status: API unreachable</h2>
        <p>{ready.reason}</p>
      </section>
    );
  }
  const r = ready.data;
  return (
    <>
      {meta.ok && meta.data.mode !== "LIVE" && (
        <p className="banner" role="status">
          {meta.data.mode} — fictional FPO data; not real farmers
        </p>
      )}
      <section className={`card ${r.status === "BLOCKED" ? "blocked" : "ready"}`} data-testid="health">
        <h2>System status: {LABEL[r.status]}</h2>
        <ul>
          {Object.entries(r.components ?? {}).map(([name, s]) => (
            <li key={name}>
              {name}: <strong>{LABEL[s]}</strong>
            </li>
          ))}
        </ul>
        {r.issues.map((i) => (
          <p key={i.code + (i.path ?? "")} className="issue">
            {i.code}: {i.message}
          </p>
        ))}
        <p className="muted">Checked {r.evaluated_at}</p>
        {meta.ok && (
          <p className="muted">
            API {meta.data.api_version} · core {meta.data.core_version} · contracts {meta.data.contracts.join(", ")}
          </p>
        )}
      </section>
    </>
  );
}

export default function Home() {
  return (
    <main>
      <h1>KisanNiti</h1>
      <p className="muted">Fair selling plans for Farmer Producer Organizations</p>
      <Suspense fallback={<p className="muted">Checking system status…</p>}>
        <Status />
      </Suspense>
    </main>
  );
}
