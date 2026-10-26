import { Suspense } from "react";
import { notFound } from "next/navigation";
import { Protected } from "@/components/Protected";
import { hasLocale } from "@/i18n/config";
import { getDictionary } from "@/i18n/dictionaries";
import { serverGet } from "@/lib/session";

interface QueueItem {
  id: string;
  plan_id: string;
  member_id: string;
  kind: string;
  status: string;
  issues: string[];
  delivery: { state: string };
}
interface Template {
  id: string;
  version: number;
  languages: { language: string; review_status: string }[];
}

export default async function MessagesPage({ params }: { params: Promise<{ lang: string }> }) {
  const { lang } = await params;
  if (!hasLocale(lang)) notFound();
  const d = await getDictionary(lang);
  return (
    <Suspense fallback={<p className="muted loading">{d.common.loading}</p>}>
      <Protected lang={lang}>
        {async (_p, dict) => {
          const [queue, templates, members] = await Promise.all([
            serverGet<QueueItem[]>("/instruction-queue"),
            serverGet<Template[]>("/message-templates"),
            serverGet<{ id: string; display_name: string }[]>("/members"),
          ]);
          const t = dict.messages;
          const names = new Map((members ?? []).map((m) => [m.id, m.display_name]));
          const issues: Record<string, string> = t.issues;
          const delivery: Record<string, string> = t.delivery;
          const kinds: Record<string, string> = t.kinds;
          const review: Record<string, string> = t.reviewStatus;
          const langs: Record<string, string> = dict.members.langs;
          return (
            <>
              <h1>{t.title}</h1>
              <p className="muted">{t.intro}</p>
              <h2>{t.queueTitle}</h2>
              {(queue ?? []).length === 0 ? (
                <p className="muted">{t.queueEmpty}</p>
              ) : (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>{t.farmer}</th>
                        <th>{t.status}</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {(queue ?? []).map((q) => (
                        <tr key={q.id}>
                          <td data-label={t.farmer}>
                            {names.get(q.member_id) ?? q.member_id} <span className="muted">· {kinds[q.kind]}</span>
                          </td>
                          <td data-label={t.status}>
                            {q.status === "BLOCKED" ? t.statuses.BLOCKED : delivery[q.delivery.state]}
                            <ul className="plain">
                              {q.issues.map((i) => (
                                <li key={i} className="issue">
                                  {issues[i] ?? i}
                                </li>
                              ))}
                            </ul>
                          </td>
                          <td>
                            <a href={`/${lang}/plans/${q.plan_id}`}>{t.openPlan}</a>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <h2>{t.templatesTitle}</h2>
              <ul className="plain">
                {(templates ?? []).map((tp) => (
                  <li key={tp.id}>
                    <strong>{kinds[tp.id]}</strong> · {t.version} {tp.version}
                    <ul className="plain muted">
                      {tp.languages.map((l) => (
                        <li key={l.language}>
                          {langs[l.language] ?? l.language}: {review[l.review_status]}
                        </li>
                      ))}
                    </ul>
                  </li>
                ))}
              </ul>
            </>
          );
        }}
      </Protected>
    </Suspense>
  );
}
