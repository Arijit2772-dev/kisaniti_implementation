import { format, type Dictionary } from "@/i18n/dictionaries";
import { getMeta, getReadiness, type ReadinessStatus } from "@/lib/health";

const componentLabel = (d: Dictionary, name: string) => (d.status.components as Record<string, string>)[name] ?? name;
const issueText = (d: Dictionary, code: string, fallback: string) => (d.status.issues as Record<string, string>)[code] ?? fallback;

export async function StatusCard({ d }: { d: Dictionary }) {
  const [ready, meta] = await Promise.all([getReadiness(), getMeta()]);
  if (!ready.ok) {
    return (
      <section className="card blocked" data-testid="health">
        <h2>{d.status.unreachable}</h2>
        <p>{ready.reason}</p>
      </section>
    );
  }
  const r = ready.data;
  const state = (s: ReadinessStatus) => d.status.state[s];
  return (
    <>
      {meta.ok && meta.data.mode !== "LIVE" && (
        <p className="banner" role="status">
          {format(d.status.demoBanner, { mode: meta.data.mode })}
        </p>
      )}
      <section className={`card ${r.status === "BLOCKED" ? "blocked" : "ready"}`} data-testid="health">
        <h2>
          {d.status.title}: {state(r.status)}
        </h2>
        <ul>
          {Object.entries(r.components ?? {}).map(([name, s]) => (
            <li key={name}>
              {componentLabel(d, name)}: <strong>{state(s)}</strong>
            </li>
          ))}
        </ul>
        {r.issues.map((i) => (
          <p key={i.code + (i.path ?? "")} className="issue">
            {i.code}: {issueText(d, i.code, i.message)}
          </p>
        ))}
        <p className="muted">
          {d.status.checkedAt}: {r.evaluated_at}
        </p>
        {meta.ok && (
          <p className="muted">
            {format(d.status.versions, {
              api: meta.data.api_version,
              core: meta.data.core_version,
              contracts: meta.data.contracts.join(", "),
            })}
          </p>
        )}
      </section>
    </>
  );
}
