"use client";
import { Fragment, useCallback, useRef, useState } from "react";
import { Flash, flashFrom, type FlashMessage } from "@/components/Flash";
import type { Dictionary } from "@/i18n/dictionaries";
import { api, problemText, type ApiProblem } from "@/lib/api-client";

type Text = Record<string, string>;
export interface MemberRef {
  id: string;
  display_name: string;
  active: boolean;
}
export interface CommodityRef {
  id: string;
  source_name: string;
}
export interface LocationRow {
  id: string;
  name: string;
  kind: string;
  village: string | null;
}
export interface LotRow {
  id: string;
  member_id: string;
  commodity_id: string;
  source_variety: string | null;
  registered_quantity_kg: string;
  harvested_on: string | null;
  current_location_id: string | null;
  balance: { on_hand_kg: string; reserved_kg: string; available_kg: string; sold_kg: string };
}
interface StockEvent {
  id: number;
  kind: string;
  quantity_kg: string | null;
  quantity_delta_kg: string | null;
  reason_code: string | null;
  created_at: string;
}

const QTY = /^\d+\.\d{3}$/;
const DELTA = /^-?\d+\.\d{3}$/;
const newKey = () => crypto.randomUUID();

export function LotsPanel({
  csrf,
  canWrite,
  t,
  errors,
  initialLots,
  initialLocations,
  members,
  commodities,
}: {
  csrf: string;
  canWrite: boolean;
  t: Dictionary["lots"];
  errors: Text;
  initialLots: LotRow[];
  initialLocations: LocationRow[];
  members: MemberRef[];
  commodities: CommodityRef[];
}) {
  const [lots, setLots] = useState(initialLots);
  const [locations, setLocations] = useState(initialLocations);
  const [message, setMessage] = useState<FlashMessage | null>(null);
  const [open, setOpen] = useState<{ id: string; what: "adjust" | "history" } | null>(null);
  const [events, setEvents] = useState<StockEvent[]>([]);
  const [lotForm, setLotForm] = useState({ member_id: "", commodity_id: "", variety: "", qty: "", harvested: "", location: "", verified: false });
  const [adj, setAdj] = useState({ delta: "", reason: "WEIGHING_CORRECTION" });
  const [place, setPlace] = useState<Record<string, string>>({});
  const [locForm, setLocForm] = useState({ name: "", kind: "COLLECTION_CENTRE" });
  const [busy, setBusy] = useState(false);
  // One key per form submission attempt: a retry after a lost answer replays instead of registering twice.
  const lotKey = useRef(newKey());
  const adjKey = useRef(newKey());

  const memberName = (id: string) => members.find((m) => m.id === id)?.display_name ?? id;
  const commodityName = (id: string) => commodities.find((c) => c.id === id)?.source_name ?? id;
  const locationName = (id: string | null) => (id ? (locations.find((l) => l.id === id)?.name ?? id) : t.noLocation);
  const kinds2: Text = t.kinds2;
  const reasons: Text = t.reasons;
  const eventKinds: Text = t.kinds;

  const fail = useCallback(
    (p: ApiProblem) => {
      let text = problemText(errors, p);
      if (p.issueCode === "STOCK_INVARIANT") text = t.refused;
      else if (p.code === "VALIDATION_FAILED" && p.issueCode?.startsWith("QUANTITY")) text = t.badQuantity;
      setMessage(flashFrom(text, p));
    },
    [errors, t.refused, t.badQuantity],
  );

  const reload = useCallback(async () => {
    const [l, loc] = await Promise.all([api<LotRow[]>("/lots"), api<LocationRow[]>("/locations")]);
    if (l.ok) setLots(l.data);
    else fail(l.problem);
    if (loc.ok) setLocations(loc.data);
  }, [fail]);

  async function createLot(e: React.FormEvent) {
    e.preventDefault();
    setMessage(null);
    if (!QTY.test(lotForm.qty)) return setMessage({ text: t.badQuantity, ref: "" });
    setBusy(true);
    const r = await api("/lots", {
      method: "POST",
      csrf,
      idempotencyKey: lotKey.current,
      body: {
        member_id: lotForm.member_id,
        commodity_id: lotForm.commodity_id,
        source_variety: lotForm.variety.trim() || null,
        registered_quantity_kg: lotForm.qty,
        harvested_on: lotForm.harvested || null,
        location_id: lotForm.location || null,
        quantity_verified: lotForm.verified,
      },
    });
    setBusy(false);
    if (r.ok) {
      lotKey.current = newKey();
      setLotForm({ ...lotForm, qty: "", variety: "", harvested: "", verified: false });
      await reload();
    } else fail(r.problem);
  }

  async function applyAdjust(lotId: string, e: React.FormEvent) {
    e.preventDefault();
    setMessage(null);
    if (!DELTA.test(adj.delta)) return setMessage({ text: t.badQuantity, ref: "" });
    const r = await api(`/lots/${lotId}/adjustments`, {
      method: "POST",
      csrf,
      idempotencyKey: adjKey.current,
      body: { quantity_delta_kg: adj.delta, reason_code: adj.reason },
    });
    if (r.ok) {
      adjKey.current = newKey();
      setAdj({ ...adj, delta: "" });
      setOpen(null);
      await reload();
    } else fail(r.problem);
  }

  async function showHistory(lotId: string) {
    if (open?.id === lotId && open.what === "history") return setOpen(null);
    const r = await api<StockEvent[]>(`/lots/${lotId}/events`);
    if (r.ok) {
      setEvents(r.data);
      setOpen({ id: lotId, what: "history" });
    } else fail(r.problem);
  }

  async function move(lotId: string) {
    const loc = place[lotId];
    if (!loc) return;
    setMessage(null);
    const r = await api(`/lots/${lotId}/placements`, { method: "POST", csrf, body: { location_id: loc } });
    if (r.ok) await reload();
    else fail(r.problem);
  }

  async function addLocation(e: React.FormEvent) {
    e.preventDefault();
    setMessage(null);
    const r = await api("/locations", { method: "POST", csrf, body: { name: locForm.name.trim(), kind: locForm.kind } });
    if (r.ok) {
      setLocForm({ ...locForm, name: "" });
      await reload();
    } else fail(r.problem);
  }

  const activeMembers = members.filter((m) => m.active);

  return (
    <>
      <Flash message={message} referenceLabel={errors.reference} />
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>{t.member}</th>
              <th>{t.commodity}</th>
              <th>{t.registered}</th>
              <th>{t.onHand}</th>
              <th>{t.reserved}</th>
              <th>{t.available}</th>
              <th>{t.sold}</th>
              <th>{t.location}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {lots.map((l) => (
              <Fragment key={l.id}>
                <tr>
                  <td data-label={t.member}>{memberName(l.member_id)}</td>
                  <td data-label={t.commodity}>
                    {commodityName(l.commodity_id)}
                    {l.source_variety && <span className="muted"> · {l.source_variety}</span>}
                  </td>
                  <td data-label={t.registered}>{l.registered_quantity_kg}</td>
                  <td data-label={t.onHand}>{l.balance.on_hand_kg}</td>
                  <td data-label={t.reserved}>{l.balance.reserved_kg}</td>
                  <td data-label={t.available}>{l.balance.available_kg}</td>
                  <td data-label={t.sold}>{l.balance.sold_kg}</td>
                  <td data-label={t.location}>
                    {locationName(l.current_location_id)}
                    {canWrite && locations.length > 0 && (
                      <span className="inline-move">
                        <select aria-label={t.place} value={place[l.id] ?? ""} onChange={(e) => setPlace({ ...place, [l.id]: e.target.value })}>
                          <option value="" />
                          {locations.map((x) => (
                            <option key={x.id} value={x.id}>
                              {x.name}
                            </option>
                          ))}
                        </select>
                        <button type="button" className="btn secondary" disabled={!place[l.id]} onClick={() => void move(l.id)}>
                          {t.changePlace}
                        </button>
                      </span>
                    )}
                  </td>
                  <td className="row-actions">
                    {canWrite && (
                      <button type="button" className="btn secondary" onClick={() => setOpen(open?.id === l.id && open.what === "adjust" ? null : { id: l.id, what: "adjust" })}>
                        {t.adjust}
                      </button>
                    )}
                    <button type="button" className="btn secondary" onClick={() => void showHistory(l.id)}>
                      {open?.id === l.id && open.what === "history" ? t.hide : t.history}
                    </button>
                  </td>
                </tr>
                {open?.id === l.id && open.what === "adjust" && (
                  <tr>
                    <td colSpan={9}>
                      <form className="form" noValidate onSubmit={(e) => void applyAdjust(l.id, e)}>
                        <label>
                          {t.delta}
                          <input inputMode="decimal" autoComplete="off" value={adj.delta} onChange={(e) => setAdj({ ...adj, delta: e.target.value })} />
                          <span className="muted">{t.deltaHint}</span>
                        </label>
                        <label>
                          {t.reason}
                          <select value={adj.reason} onChange={(e) => setAdj({ ...adj, reason: e.target.value })}>
                            {Object.entries(reasons).map(([k, v]) => (
                              <option key={k} value={k}>
                                {v}
                              </option>
                            ))}
                          </select>
                        </label>
                        <button type="submit" className="btn" disabled={!adj.delta}>
                          {t.apply}
                        </button>
                      </form>
                    </td>
                  </tr>
                )}
                {open?.id === l.id && open.what === "history" && (
                  <tr>
                    <td colSpan={9}>
                      <ul className="plain">
                        {events.map((ev) => (
                          <li key={ev.id}>
                            <span className="muted">{ev.created_at}</span> · {eventKinds[ev.kind] ?? ev.kind} ·{" "}
                            <strong>{ev.quantity_delta_kg ?? ev.quantity_kg}</strong>
                            {ev.reason_code && <span className="muted"> · {reasons[ev.reason_code] ?? ev.reason_code}</span>}
                          </li>
                        ))}
                      </ul>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
        {lots.length === 0 && <p className="muted">{t.empty}</p>}
      </div>

      {canWrite && (
        <>
          <h2>{t.add}</h2>
          {activeMembers.length === 0 ? (
            <p className="muted">{t.needMember}</p>
          ) : (
            <form onSubmit={createLot} className="form" noValidate>
              <label>
                {t.member}
                <select required value={lotForm.member_id} onChange={(e) => setLotForm({ ...lotForm, member_id: e.target.value })}>
                  <option value="" />
                  {activeMembers.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.display_name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                {t.commodity}
                <select required value={lotForm.commodity_id} onChange={(e) => setLotForm({ ...lotForm, commodity_id: e.target.value })}>
                  <option value="" />
                  {commodities.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.source_name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                {t.variety}
                <input maxLength={60} value={lotForm.variety} onChange={(e) => setLotForm({ ...lotForm, variety: e.target.value })} />
              </label>
              <label>
                {t.quantity}
                <input required inputMode="decimal" autoComplete="off" value={lotForm.qty} onChange={(e) => setLotForm({ ...lotForm, qty: e.target.value })} />
                <span className="muted">{t.quantityHint}</span>
              </label>
              <label>
                {t.harvested}
                <input type="date" value={lotForm.harvested} onChange={(e) => setLotForm({ ...lotForm, harvested: e.target.value })} />
              </label>
              <label>
                {t.location}
                <select value={lotForm.location} onChange={(e) => setLotForm({ ...lotForm, location: e.target.value })}>
                  <option value="">{t.noLocation}</option>
                  {locations.map((x) => (
                    <option key={x.id} value={x.id}>
                      {x.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="check">
                <input type="checkbox" checked={lotForm.verified} onChange={(e) => setLotForm({ ...lotForm, verified: e.target.checked })} /> {t.verified}
              </label>
              <button type="submit" className="btn" disabled={busy || !lotForm.member_id || !lotForm.commodity_id || !lotForm.qty}>
                {busy ? t.adding : t.submit}
              </button>
            </form>
          )}
        </>
      )}

      <h2>{t.locationsTitle}</h2>
      <ul className="plain">
        {locations.map((x) => (
          <li key={x.id}>
            <strong>{x.name}</strong> <span className="muted">· {kinds2[x.kind] ?? x.kind}</span>
          </li>
        ))}
      </ul>
      {locations.length === 0 && <p className="muted">{t.noLocations}</p>}
      {canWrite && (
        <>
          <h3>{t.addLocation}</h3>
          <form onSubmit={addLocation} className="form" noValidate>
            <label>
              {t.locationName}
              <input required maxLength={120} value={locForm.name} onChange={(e) => setLocForm({ ...locForm, name: e.target.value })} />
            </label>
            <label>
              {t.locationKind}
              <select value={locForm.kind} onChange={(e) => setLocForm({ ...locForm, kind: e.target.value })}>
                {Object.entries(kinds2).map(([k, v]) => (
                  <option key={k} value={k}>
                    {v}
                  </option>
                ))}
              </select>
            </label>
            <button type="submit" className="btn" disabled={!locForm.name.trim()}>
              {t.submitLocation}
            </button>
          </form>
        </>
      )}
    </>
  );
}
