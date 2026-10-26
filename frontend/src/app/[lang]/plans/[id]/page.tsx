import { Suspense } from "react";
import { notFound } from "next/navigation";
import { HandoverPanel, type EarlierHandover } from "@/components/HandoverPanel";
import { InstructionsPanel, type Instruction } from "@/components/InstructionsPanel";
import { PlanReview, type Choice, type Context, type PlanState, type Revision } from "@/components/PlanReview";
import { Protected } from "@/components/Protected";
import { hasLocale } from "@/i18n/config";
import { format } from "@/i18n/dictionaries";
import { serverGet } from "@/lib/session";

type Params = Record<string, string>;
interface Item {
  code: string;
  params: Params;
}
interface Plan {
  id: string;
  created_at: string;
  variety: string;
  status: string;
  not_proposed_reasons: string[];
  dataset: { validation_status: string; name: string };
  origin_date: string;
  mandis: { mandi_id: string; name: string }[];
  lots: {
    lot_id: string;
    member_id: string;
    available_kg: string;
    planned_kg: string | null;
    excluded_reason: string | null;
    storage: string;
    storage_reason: string | null;
  }[];
  allocation_input_sha256: string;
  allocation_input: { choices: { lot_id: string; mandi_id: string; date: string }[] };
  allocation_result: {
    assignments: { lot_id: string; mandi_id: string; date: string; quantity_kg: string }[];
    farmers: { farmer_id: string; fulfilment: { numerator_kg: string; denominator_kg: string; display: string } }[];
    totals: { participating_kg: string; scheduled_kg: string; gross_inr: string; cost_inr: string; expected_net_return_inr: string };
    status: { optimality: string; termination: string };
  };
  warnings: Item[];
  explanation: Item[];
  policy_versions: Params;
}
interface Execution {
  status: string;
  lines: { lot_id: string; mandi_id: string; date: string; planned_kg: string; sold_kg: string; status: string; deviation_kg: string }[];
  lots: { lot_id: string; reserved_kg: string; consumed_kg: string; released_kg: string; released_for_sale_kg: string; remaining_kg: string; off_plan_sold_kg: string }[];
}
interface Report {
  lines: {
    lot_id: string;
    mandi_id: string;
    date: string;
    planned_kg: string;
    sold_kg: string;
    planned_price_inr_per_quintal: string;
    actual_avg_price_inr_per_quintal: string | null;
    price_gap_inr_per_quintal: string | null;
    planned_value_inr: string;
    actual_value_inr: string;
  }[];
  farmers: { farmer_id: string; planned_ratio: { display: string }; actual_ratio: { display: string } }[];
  totals: { planned_kg: string; sold_kg: string; off_plan_kg: string; planned_value_inr: string; actual_value_inr: string };
  lowest_planned_display: string | null;
  lowest_actual_display: string | null;
}
interface Member {
  id: string;
  display_name: string;
}

// The plan id is runtime data, so it is read inside Suspense (Cache Components): the shell prerenders, the plan streams.
export default function PlanPage({ params }: { params: Promise<{ lang: string; id: string }> }) {
  return (
    <Suspense fallback={<p className="muted loading">…</p>}>
      <PlanContent params={params} />
    </Suspense>
  );
}

async function PlanContent({ params }: { params: Promise<{ lang: string; id: string }> }) {
  const { lang, id } = await params;
  if (!hasLocale(lang)) notFound();
  return (
    <>
      <Protected lang={lang}>
        {async (p, dict) => {
          const pid = encodeURIComponent(id);
          const [plan, members, state, revisions, context, users, instructions, execution, report, handovers] = await Promise.all([
            serverGet<Plan>(`/plans/${pid}`),
            serverGet<Member[]>("/members"),
            serverGet<PlanState>(`/plans/${pid}/state`),
            serverGet<Revision[]>(`/plans/${pid}/revisions`),
            serverGet<Context>(`/plans/${pid}/approval-context`),
            p.capabilities.includes("users.manage") ? serverGet<{ id: string; display_name: string }[]>("/users") : Promise.resolve(null),
            serverGet<Instruction[]>(`/plans/${pid}/instructions`),
            serverGet<Execution>(`/plans/${pid}/execution`),
            serverGet<Report>(`/plans/${pid}/report`),
            serverGet<EarlierHandover[]>(`/plans/${pid}/handovers`),
          ]);
          if (!plan || !state || !revisions || !context) notFound();
          const t = dict.plans;
          const memberName = new Map((members ?? []).map((m) => [m.id, m.display_name]));
          const mandiName = new Map(plan.mandis.map((m) => [m.mandi_id, m.name]));
          const lotById = new Map(plan.lots.map((l) => [l.lot_id, l]));
          const lotLabel = (lotId: string) => {
            const l = lotById.get(lotId);
            return l ? `${memberName.get(l.member_id) ?? l.member_id} · ${l.available_kg} kg` : lotId;
          };
          const reasons: Params = t.reasons;
          const categories: Params = t.categories;
          const statusNames: Params = dict.prices.status;
          const explain: Params = t.explain;
          const warnTexts: Params = t.warnings;
          const resolve = (p: Params): Params => {
            const out: Params = { ...p };
            if (p.mandi_id) out.mandi = mandiName.get(p.mandi_id) ?? p.mandi_id;
            if (p.lot_id) out.lot = lotLabel(p.lot_id);
            if (p.farmer_id) out.farmer = memberName.get(p.farmer_id) ?? p.farmer_id;
            if (p.reason) out.reason = reasons[p.reason] ?? p.reason;
            if (p.category) out.category = categories[p.category] ?? p.category;
            if (p.status) out.status = statusNames[p.status] ?? p.status;
            return out;
          };
          const currentRev = revisions[revisions.length - 1];
          const res = currentRev.allocation_result as Plan["allocation_result"];
          const lotLabels: Params = Object.fromEntries(plan.lots.map((l) => [l.lot_id, lotLabel(l.lot_id)]));
          const mandiNames: Params = Object.fromEntries(plan.mandis.map((m) => [m.mandi_id, m.name]));
          const userNames: Params = Object.fromEntries([[p.user_id, p.display_name], ...(users ?? []).map((u) => [u.id, u.display_name])]);
          const planWarningTexts: Params = {};
          for (const w of plan.warnings) planWarningTexts[w.code] ??= format(warnTexts[w.code] ?? w.code, resolve(w.params));
          const left = (Number(res.totals.participating_kg) - Number(res.totals.scheduled_kg)).toFixed(3);
          const proposed = state.status === "PROPOSED" || state.status === "APPROVED";
          return (
            <>
              <p>
                <a href={`/${lang}/plans`}>← {t.back}</a>
              </p>
              <h1>
                {t.title}: {plan.variety}
              </h1>
              <p className="muted">
                {t.planId} {plan.id} · {plan.created_at}
              </p>
              <section className={proposed ? "card ready" : "card blocked"}>
                <p>
                  <strong>{(dict.review.statusNames as Params)[state.status] ?? state.status}</strong>
                </p>
                {!proposed && (
                  <>
                    <p>{t.notProposedTitle}:</p>
                    <ul>
                      {plan.not_proposed_reasons.map((r) => (
                        <li key={r}>{(t.notProposedReasons as Params)[r] ?? r}</li>
                      ))}
                    </ul>
                  </>
                )}
                {state.status === "PROPOSED" && <p role="note">{t.notApproved}</p>}
              </section>

              <PlanReview
                csrf={p.csrf_token}
                canWrite={p.capabilities.includes("data.write")}
                canApprove={p.capabilities.includes("plans.approve")}
                t={dict.review}
                planWarningTexts={planWarningTexts}
                errors={dict.errors}
                planId={plan.id}
                state={state}
                revisions={revisions}
                context={context}
                choices={plan.allocation_input.choices.map((c): Choice => ({ lot_id: c.lot_id, mandi_id: c.mandi_id, date: c.date }))}
                lotLabels={lotLabels}
                mandiNames={mandiNames}
                userNames={userNames}
                categories={categories}
              />

              {plan.warnings.length > 0 && (
                <>
                  <h2>{t.warningsTitle}</h2>
                  <ul>
                    {plan.warnings.map((w, i) => (
                      <li key={`${w.code}-${i}`}>{format(warnTexts[w.code] ?? w.code, resolve(w.params))}</li>
                    ))}
                  </ul>
                </>
              )}

              {state.status === "APPROVED" && (
                <HandoverPanel
                  csrf={p.csrf_token}
                  canSend={p.capabilities.includes("messages.send")}
                  canLink={p.capabilities.includes("contacts.view_phone")}
                  t={dict.handover}
                  langs={dict.members.langs}
                  issueTexts={dict.messages.issues}
                  errors={dict.errors}
                  planId={plan.id}
                  reservation={state.reservation.state}
                  earlier={handovers ?? []}
                />
              )}

              {state.status === "APPROVED" && (
                <InstructionsPanel
                  csrf={p.csrf_token}
                  canSend={p.capabilities.includes("messages.send")}
                  t={dict.messages}
                  langs={dict.members.langs}
                  errors={dict.errors}
                  planId={plan.id}
                  reservation={state.reservation.state}
                  memberNames={Object.fromEntries((members ?? []).map((m) => [m.id, m.display_name]))}
                  initial={instructions ?? []}
                />
              )}

              {state.status === "APPROVED" && execution && report && (
                <>
                  <h2>{dict.execution.title}</h2>
                  <p>
                    <strong>{(dict.execution.status as Params)[execution.status]}</strong>
                    {execution.lots.every((l) => Number(l.remaining_kg) === 0) && <span className="muted"> · {dict.execution.fullySold}</span>}
                  </p>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>{dict.execution.line}</th>
                          <th>{dict.execution.planned}</th>
                          <th>{dict.execution.sold}</th>
                          <th>{dict.execution.deviation}</th>
                          <th />
                        </tr>
                      </thead>
                      <tbody>
                        {execution.lines.map((l) => (
                          <tr key={`${l.lot_id}${l.mandi_id}${l.date}`}>
                            <td data-label={dict.execution.line}>
                              {lotLabel(l.lot_id)} · {mandiName.get(l.mandi_id) ?? l.mandi_id} · {l.date}
                            </td>
                            <td data-label={dict.execution.planned}>{l.planned_kg}</td>
                            <td data-label={dict.execution.sold}>{l.sold_kg}</td>
                            <td data-label={dict.execution.deviation}>{l.deviation_kg}</td>
                            <td>{(dict.execution.status as Params)[l.status]}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>{dict.execution.lot}</th>
                          <th>{dict.execution.reserved}</th>
                          <th>{dict.execution.consumed}</th>
                          <th>{dict.execution.released}</th>
                          <th>{dict.execution.releasedForSale}</th>
                          <th>{dict.execution.remaining}</th>
                          <th>{dict.execution.offPlanSold}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {execution.lots.map((l) => (
                          <tr key={l.lot_id}>
                            <td data-label={dict.execution.lot}>{lotLabel(l.lot_id)}</td>
                            <td data-label={dict.execution.reserved}>{l.reserved_kg}</td>
                            <td data-label={dict.execution.consumed}>{l.consumed_kg}</td>
                            <td data-label={dict.execution.released}>{l.released_kg}</td>
                            <td data-label={dict.execution.releasedForSale}>{l.released_for_sale_kg}</td>
                            <td data-label={dict.execution.remaining}>{l.remaining_kg}</td>
                            <td data-label={dict.execution.offPlanSold}>{l.off_plan_sold_kg}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <h2>{dict.execution.reportTitle}</h2>
                  <p className="muted">{dict.execution.reportLabel}</p>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>{dict.execution.line}</th>
                          <th>{dict.execution.planPrice}</th>
                          <th>{dict.execution.actualPrice}</th>
                          <th>{dict.execution.gap}</th>
                          <th>{dict.execution.plannedValue}</th>
                          <th>{dict.execution.actualValue}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {report.lines.map((l) => (
                          <tr key={`${l.lot_id}${l.mandi_id}${l.date}`}>
                            <td data-label={dict.execution.line}>
                              {lotLabel(l.lot_id)} · {l.date}
                            </td>
                            <td data-label={dict.execution.planPrice}>{l.planned_price_inr_per_quintal}</td>
                            <td data-label={dict.execution.actualPrice}>{l.actual_avg_price_inr_per_quintal ?? "—"}</td>
                            <td data-label={dict.execution.gap}>{l.price_gap_inr_per_quintal ?? "—"}</td>
                            <td data-label={dict.execution.plannedValue}>{l.planned_value_inr}</td>
                            <td data-label={dict.execution.actualValue}>{l.actual_value_inr}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <ul className="plain">
                    <li>
                      {dict.execution.plannedValue}: <strong>{report.totals.planned_value_inr}</strong> · {dict.execution.actualValue}: <strong>{report.totals.actual_value_inr}</strong>
                    </li>
                    <li>
                      {dict.execution.offPlanTotal}: <strong>{report.totals.off_plan_kg}</strong>
                    </li>
                  </ul>
                  <h3>{dict.execution.fairness}</h3>
                  <ul className="plain">
                    {report.farmers.map((f) => (
                      <li key={f.farmer_id}>
                        {memberName.get(f.farmer_id) ?? f.farmer_id}: {dict.execution.plannedShare} <strong>{f.planned_ratio.display}</strong> · {dict.execution.soldShare} <strong>{f.actual_ratio.display}</strong>
                      </li>
                    ))}
                    <li className="muted">
                      {dict.execution.lowestPlanned}: {report.lowest_planned_display ?? "—"} · {dict.execution.lowestActual}: {report.lowest_actual_display ?? "—"}
                    </li>
                  </ul>
                </>
              )}

              <h2>{t.assignments}</h2>
              <p className="muted">{format(dict.review.revision, { n: String(currentRev.revision_no) })}</p>
              {res.assignments.length === 0 ? (
                <p className="muted">{t.nothing}</p>
              ) : (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>{t.farmer}</th>
                        <th>{t.lot}</th>
                        <th>{t.mandi}</th>
                        <th>{t.date}</th>
                        <th>{t.quantity}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {res.assignments.map((a) => (
                        <tr key={`${a.lot_id}-${a.mandi_id}-${a.date}`}>
                          <td data-label={t.farmer}>{memberName.get(lotById.get(a.lot_id)?.member_id ?? "") ?? ""}</td>
                          <td data-label={t.lot}>{lotLabel(a.lot_id)}</td>
                          <td data-label={t.mandi}>{mandiName.get(a.mandi_id) ?? a.mandi_id}</td>
                          <td data-label={t.date}>{a.date}</td>
                          <td data-label={t.quantity}>{a.quantity_kg}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              <h2>{t.totals}</h2>
              <ul className="plain">
                <li>
                  {t.planned_kg}: <strong>{res.totals.scheduled_kg} kg</strong>
                </li>
                <li>
                  {t.left_kg}: <strong>{left} kg</strong>
                </li>
                <li>
                  {t.gross}: <strong>{res.totals.gross_inr}</strong>
                </li>
                <li>
                  {t.cost}: <strong>{res.totals.cost_inr}</strong>
                </li>
                <li>
                  {t.netTotal}: <strong>{res.totals.expected_net_return_inr}</strong>
                </li>
              </ul>

              <h2>{t.share}</h2>
              <ul className="plain">
                {res.farmers.map((f) => (
                  <li key={f.farmer_id}>
                    {memberName.get(f.farmer_id) ?? f.farmer_id}: <strong>{f.fulfilment.display}</strong>{" "}
                    <span className="muted">
                      ({f.fulfilment.numerator_kg} / {f.fulfilment.denominator_kg} kg)
                    </span>
                  </li>
                ))}
              </ul>

              <h2>{t.explainTitle}</h2>
              <ul>
                {plan.explanation.map((x, i) => (
                  <li key={`${x.code}-${i}`}>{format(explain[x.code] ?? x.code, resolve(x.params))}</li>
                ))}
              </ul>

              <h2>{t.lotsTitle}</h2>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>{t.lot}</th>
                      <th>{t.planBasis}</th>
                      <th>{t.storage}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {plan.lots.map((l) => (
                      <tr key={l.lot_id}>
                        <td data-label={t.lot}>{lotLabel(l.lot_id)}</td>
                        <td data-label={t.planBasis}>{l.planned_kg ?? reasons[l.excluded_reason ?? ""] ?? l.excluded_reason}</td>
                        <td data-label={t.storage}>
                          {categories[l.storage] ?? l.storage}
                          {l.storage_reason && <span className="muted"> · {reasons[l.storage_reason] ?? l.storage_reason}</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <h2>{t.policiesTitle}</h2>
              <ul className="plain muted">
                {Object.entries(plan.policy_versions).map(([k, v]) => (
                  <li key={k}>
                    {k}: {v}
                  </li>
                ))}
                <li>
                  {t.fingerprint}: <code>{plan.allocation_input_sha256.slice(0, 16)}…</code>
                </li>
              </ul>
            </>
          );
        }}
      </Protected>
    </>
  );
}
