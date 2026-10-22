"use client";

import { useEffect, useRef, useState, type CSSProperties } from "react";

import { Avatar, personClass } from "@/components/People";
import { say, toMin } from "@/lib/time";
import type { Action, CalEvent, Check, Member, View } from "@/lib/types";

/* The Living Timeline: one picture of the family's morning that changes only when the server says so.
 *
 * Every mark is drawn from a field of the view the last turn returned (state, conflicts, plan, verify),
 * and keeps a stable key across turns, so a bar that the stored state moves glides from where it was to
 * where it now is. Nothing animates ahead of the data: a pending turn only dims the picture.
 *
 *   A/B   the stored morning (B: the alert has arrived but has not been checked)
 *   C     conflicts from ingest_school_notice; the plan's changes in outline under each lane ("plan")
 *   D     the stored state after apply_recovery_plan, each changed mark sealed by its verify_recovery check
 *
 * Hooks for the stage and trace work: the figure carries data-phase / data-verdict / data-mode / aria-busy;
 * every mark carries data-key, data-source (the MCP tool its data came from), data-seq (plan action) and
 * data-verified (its read-back check). */

const LO = 7 * 60;
const HI = 10 * 60 + 45;
/* Lane geometry. "dense" is for short wide screens (1440 x 810 video frame, 1280 x 800): every lane,
   Noah's included, stays above the fold. While a plan is proposed ("split") each lane keeps the morning as
   it is now as a thin bar with no words (who is with whom, the conflicts) and the plan as full blocks. */
const GEO = {
  roomy: { single: 38, split: 40, pad: 5, nowTop: 5, nowH: 7, planTop: 15, track: 22 },
  dense: { single: 33, split: 36, pad: 4, nowTop: 4, nowH: 6, planTop: 13, track: 20 },
} as const;
type Density = keyof typeof GEO;
const DENSE_QUERY = "(min-width: 1081px) and (max-height: 900px), (min-width: 1081px) and (min-aspect-ratio: 17/10)";
const STEP_MS = 60; // stagger between plan actions when the stored state lands
const MOVE_MS = 700; // how long a mark takes to land (keep in step with --lt-move in globals.css)

/** When this turn wrote the plan (apply ran with nothing already in), the marks land in plan-action order and
 *  "ready" shows once the last one is down. Any other turn (a retry, "Are we good?") draws at once. */
export function landingOf(view: View): { fresh: boolean; readyAt: number } {
  const fresh = phaseOf(view) === "written" && !!view.apply && !view.apply.skipped_already_applied;
  const maxSeq = Math.max(0, ...(view.plan?.actions ?? []).map((a) => a.seq));
  return { fresh, readyAt: fresh ? Math.max(0, maxSeq - 1) * STEP_MS + MOVE_MS + 250 : 0 };
}

type Mode = "single" | "split";
type Track = "now" | "plan" | "full";
type Kind = "commit" | "soft" | "duty" | "with" | "ride" | "gap" | "window" | "clash" | "unres" | "avail" | "away";
export type Phase = "normal" | "alert" | "conflict" | "proposed" | "no-plan" | "set-aside" | "written";

interface Item {
  key: string;
  row: number;
  track: Track;
  start: string;
  end: string;
  kind: Kind;
  label: string;
  short?: string;
  who?: string | null;
  source: string;
  ghost?: boolean; // a change the plan proposes (outline)
  settled?: boolean; // a proposed change that the stored state now holds (the outline dissolves)
  leaving?: boolean; // this mark changes if the plan is applied
  invalid?: boolean; // the school change makes this impossible
  blocker?: boolean; // why no plan covers everyone
  closed?: boolean; // a conflict the stored state no longer has
  fresh?: boolean; // written by this plan (drawn in when it lands)
  pin?: number; // conflict number
  fix?: number; // the conflict this change fixes
  seq?: number; // plan action that wrote it
  seal?: boolean | null; // its read-back check
  title: string; // full words for the tooltip and the text alternative
}

interface Lane {
  id: string;
  name: string;
  sub: string;
  row: number;
  msg?: { state: "planned" | "ok" | "failed"; seq: number; summary: string };
}

interface Link {
  key: string;
  kind: "move" | "handoff";
  from: string; // item keys
  to: string;
  via?: string[];
  who: string;
  at?: string;
  title: string;
  label?: string; // words on the line: "Drive Emma → Linda 9:55"
}

const sayShort = (t: string) => say(t).replace(/ [AP]M$/, "");
const clampMin = (t: string) => Math.min(Math.max(toMin(t), LO), HI);
const pct = (t: string) => ((clampMin(t) - LO) / (HI - LO)) * 100;
const hhmm = (m: number) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
const ranges = (text: string) => text.replace(/(\d\d:\d\d)-(\d\d:\d\d)/g, (_, a, b) => `${say(a)}–${say(b)}`);
const compact = (text: string) => text.replace(/(\d\d:\d\d)-(\d\d:\d\d)/g, (_, a, b) => `${sayShort(a)}–${sayShort(b)}`);

/** A short label for short calendar entries: "Noah" (a drive, shown with a car glyph), "Emma" (for "With Emma at home"), "Client". */
function shortTitle(title: string): string {
  if (title.startsWith("Drive ")) return title.split(" ")[1] ?? "Drive";
  const withWho = /^With (\w+)/.exec(title);
  if (withWho) return withWho[1];
  return title.split(" ")[0];
}

/** A drive keeps one key per responsibility, so when it changes hands it travels to the new lane. */
const eventKey = (e: { id: string; kind?: string; responsibility_id?: string | null }) =>
  e.kind === "duty" && e.responsibility_id?.startsWith("r-") ? `duty:${e.responsibility_id}` : `ev:${e.id}`;

function band(density: Density, mode: Mode, row: number, track: Track): { y: number; h: number } {
  const g = GEO[density];
  const H = g[mode];
  if (track === "full") return { y: row * H + 2, h: H - 4 };
  if (mode === "single") return { y: row * H + g.pad, h: H - 2 * g.pad };
  return track === "now" ? { y: row * H + g.nowTop, h: g.nowH } : { y: row * H + g.planTop, h: g.track };
}

/** Matches the dense lane geometry to the screen (client only: the timeline renders after the first fetch). */
function useDensity(): Density {
  const [dense, setDense] = useState(() => typeof window !== "undefined" && !!window.matchMedia?.(DENSE_QUERY).matches);
  useEffect(() => {
    const mq = window.matchMedia?.(DENSE_QUERY);
    if (!mq) return;
    const on = () => setDense(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return dense ? "dense" : "roomy";
}

/** A drive (car) and the place a child is due (school or daycare): marks too short for words keep a glyph. */
function CarGlyph() {
  return (
    <svg className="glyph" viewBox="0 0 16 12" aria-hidden>
      <path d="M2.2 7 3.7 3.3A1.5 1.5 0 0 1 5.1 2.3h5.8a1.5 1.5 0 0 1 1.4 1L13.8 7" />
      <rect x="1" y="6.8" width="14" height="3.4" rx="1.3" />
      <circle cx="4.6" cy="10.4" r="1.2" />
      <circle cx="11.4" cy="10.4" r="1.2" />
    </svg>
  );
}
function PlaceGlyph() {
  return (
    <svg className="glyph" viewBox="0 0 14 12" aria-hidden>
      <path d="M1.5 11V5.2L7 1.5l5.5 3.7V11Z" />
      <path d="M5.4 11V7.6h3.2V11" />
    </svg>
  );
}

export function phaseOf(view: View): Phase {
  if (view.screen === "D" && view.verify) return "written";
  if (view.screen === "C" && view.plan) {
    if (!view.plan.feasible) return "no-plan";
    if (view.plan.status === "proposed") return "proposed";
    if (view.plan.status === "rejected") return "set-aside";
    return "conflict";
  }
  if (view.screen === "C" || view.screen === "D") return view.conflicts.length ? "conflict" : "normal";
  if (view.screen === "B") return "alert";
  return "normal";
}

interface Model {
  phase: Phase;
  verdict: "ready" | "not-ready" | null;
  mode: Mode;
  lanes: Lane[];
  items: Item[];
  links: Link[];
  flags: { key: string; at: string; from: string | null; delay: number | null; label: string }[];
  readyAt: number;
}

function build(view: View): Model | null {
  const s = view.state;
  if (!s) return null;
  const phase = phaseOf(view);
  const plan = view.plan;
  const verify = view.verify;
  const written = phase === "written";
  const proposing = phase === "proposed";
  const mode: Mode = proposing ? "split" : "single";
  const acts: Action[] = (proposing || written) && plan ? plan.actions : [];
  const conflicts = phase === "normal" || phase === "alert" ? [] : plan && view.screen === "C" ? plan.conflicts : view.conflicts;

  const order = { parent: 0, caregiver: 1, child: 2 } as const;
  const members = [...s.members].sort((a, b) => order[a.role] - order[b.role]);
  const rowOf = new Map(members.map((m, i) => [m.id, i]));
  const nameOf = (id: string | null | undefined) => members.find((m) => m.id === id)?.name ?? id ?? "nobody";
  const checkOf = (seq: number): Check | undefined => verify?.checks.find((c) => c.seq === seq);
  const sealOf = (seqs: number[]): boolean | null => {
    if (!written || !seqs.length) return null;
    const cs = seqs.map(checkOf);
    return cs.some((c) => !c) ? null : cs.every((c) => c!.verified);
  };
  const tgt = (a: Action, k: string) => (a.target as Record<string, unknown>)[k];
  const bef = (a: Action, k: string) => (a.before as Record<string, unknown> | null)?.[k];
  const aft = (a: Action, k: string) => (a.after as Record<string, unknown> | null)?.[k];
  const createOf = (eventId: string) => acts.find((a) => a.type === "create_calendar_event" && tgt(a, "event_id") === eventId);
  const cancelOf = (eventId: string) => acts.find((a) => a.type === "cancel_calendar_event" && tgt(a, "event_id") === eventId);
  const cancelForResp = (resp: string | null | undefined) =>
    resp ? acts.find((a) => a.type === "cancel_calendar_event" && bef(a, "responsibility_id") === resp) : undefined;
  const escortAct = (id: string) => acts.find((a) => a.type === "assign_escort" && tgt(a, "escort_id") === id);
  const supAct = (child: string) => acts.find((a) => a.type === "set_supervision" && tgt(a, "child_id") === child);
  const notifyAct = (member: string) => acts.find((a) => a.type === "notify_member" && tgt(a, "member_id") === member);

  // conflicts, numbered as in the list above the timeline
  const clashes = new Map<string, { n: number; member: string; at: string | null; detail: string }>();
  const gaps: { child: string; iv: string; n: number; j: number }[] = [];
  conflicts.forEach((c, i) => {
    if (c.code === "OWNER_UNAVAILABLE" && c.responsibility_id && c.member_id) {
      clashes.set(c.responsibility_id, { n: i + 1, member: c.member_id, at: /at (\d\d:\d\d)/.exec(c.detail)?.[1] ?? null, detail: c.detail });
    }
    if (c.code === "SUPERVISION_GAP" && c.child_id) c.intervals.forEach((iv, j) => gaps.push({ child: c.child_id!, iv, n: i + 1, j }));
  });

  // what blocks a plan (no-plan): "Sarah: 'Work' (hard) 08:30-17:00", "Linda is unavailable today"
  const blockers = phase === "no-plan" ? (plan?.unresolved ?? []).flatMap((u) => u.blockers) : [];
  const blocks = (m: Member, e?: CalEvent) =>
    blockers.some((b) => (e ? b.startsWith(`${m.name}: '${e.title}'`) && b.includes(`${e.start}-`) : b.startsWith(`${m.name} is unavailable`)));

  const items: Item[] = [];
  const lanes: Lane[] = [];
  const stored = written ? "get_household_state (after apply_recovery_plan)" : "get_household_state";

  for (const m of members) {
    const row = rowOf.get(m.id)!;
    const sub =
      m.role === "child"
        ? m.age
          ? `age ${m.age}`
          : "child"
        : m.role === "caregiver"
          ? m.available
            ? `caregiver ${m.available_window ? compact(m.available_window) : ""}`.trim()
            : "unavailable today"
          : "parent";
    const lane: Lane = { id: m.id, name: m.name, sub, row };
    const note = notifyAct(m.id);
    if (note) {
      const c = checkOf(note.seq);
      lane.msg = {
        state: written ? (c?.verified ? "ok" : "failed") : "planned",
        seq: note.seq,
        summary: written ? (c?.verified ? `message to ${m.name} read back` : `message to ${m.name} not delivered`) : `message to ${m.name} in the plan`,
      };
    }
    lanes.push(lane);

    if (m.role !== "child") {
      if (m.role === "caregiver") {
        if (m.available && m.available_window) {
          const [a, b] = m.available_window.split("-");
          items.push({ key: `avail:${m.id}`, row, track: "full", start: a, end: b, kind: "avail", label: "free to help", who: m.id, source: stored, title: `${m.name} can help ${ranges(m.available_window)}` });
        } else if (!m.available) {
          items.push({
            key: `away:${m.id}`, row, track: "full", start: hhmm(LO), end: hhmm(HI), kind: "away", label: "unavailable today", who: m.id,
            source: stored, blocker: blocks(m), title: `${m.name} is unavailable today`,
          });
        }
      }
      for (const e of s.calendar) {
        if (e.member_id !== m.id || e.status !== "confirmed") continue;
        const key = eventKey(e);
        const create = createOf(e.id);
        const cancelPrev = create ? cancelForResp(e.responsibility_id) : undefined;
        const moved = !!create && key.startsWith("duty:") && !!cancelPrev;
        const kind: Kind = e.kind === "duty" ? "duty" : e.hard ? "commit" : "soft";
        items.push({
          key, row, track: "now", start: e.start, end: e.end, kind, label: e.title, short: shortTitle(e.title),
          who: e.kind === "duty" ? m.id : null,
          source: written && create ? "apply_recovery_plan" : stored,
          leaving: proposing && !!cancelOf(e.id),
          blocker: blocks(m, e),
          fresh: written && !!create && !moved,
          seq: written && create ? create.seq : undefined,
          seal: create ? sealOf([create.seq, ...(cancelPrev ? [cancelPrev.seq] : [])]) : null,
          title: `${m.name}: ${e.title} ${say(e.start)}–${say(e.end)}${kind === "soft" ? " (can move)" : kind === "commit" ? " (fixed)" : ""}`,
        });
      }
      // the moment the school change asks of an adult who cannot do it
      for (const [resp, c] of clashes) {
        if (c.member !== m.id) continue;
        const esc = s.escorts.find((x) => x.id === resp);
        const at = c.at ?? esc?.window.split("-")[0];
        if (!at || !esc) continue;
        const end = toMin(esc.target_arrival) > toMin(at) ? esc.target_arrival : hhmm(toMin(at) + 10);
        const act = escortAct(resp);
        items.push({
          key: `clash:${resp}`, row, track: "now", start: at, end, kind: "clash", label: "", short: "",
          source: "ingest_school_notice", pin: c.n, closed: written && esc.assigned_member_id !== m.id,
          seq: written && act ? act.seq : undefined,
          title: `Conflict ${c.n}: ${ranges(c.detail)}`,
        });
      }
    } else {
      for (const esc of s.escorts.filter((x) => x.child_id === m.id)) {
        const [a, b] = esc.window.split("-");
        items.push({
          key: `win:${esc.id}`, row, track: "full", start: a, end: b, kind: "window", label: esc.destination, source: view.notice ? "ingest_school_notice" : stored,
          title: `${m.name} due at ${esc.destination} ${say(a)}–${say(b)}`,
        });
      }
      const sup = s.supervision.filter((x) => x.child_id === m.id);
      const sAct = supAct(m.id);
      const before = ((sAct?.before as { segments?: unknown[] } | null)?.segments ?? []).length;
      sup.forEach((seg, i) => {
        items.push({
          key: `sup:${m.id}:${i}`, row, track: "now", start: seg.start, end: seg.end, kind: "with",
          label: `with ${nameOf(seg.member_id)}`, short: nameOf(seg.member_id), who: seg.member_id,
          source: written && sAct ? "apply_recovery_plan" : stored,
          leaving: proposing && !!sAct,
          fresh: written && !!sAct && i >= before,
          seq: written && sAct ? sAct.seq : undefined,
          seal: sAct && i === sup.length - 1 ? sealOf([sAct.seq]) : null,
          title: `${m.name} with ${nameOf(seg.member_id)} ${say(seg.start)}–${say(seg.end)}`,
        });
      });
      for (const esc of s.escorts.filter((x) => x.child_id === m.id)) {
        if (!esc.depart || !esc.arrive) continue;
        const clash = clashes.get(esc.id);
        const eAct = escortAct(esc.id);
        const invalid = !!clash && !written && esc.assigned_member_id === clash.member;
        items.push({
          key: `ride:${esc.id}`, row, track: "now", start: esc.depart, end: esc.arrive, kind: "ride",
          label: `${nameOf(esc.assigned_member_id)} drives`, short: nameOf(esc.assigned_member_id), who: esc.assigned_member_id,
          source: written && eAct ? "apply_recovery_plan" : stored,
          invalid, pin: invalid ? clash!.n : undefined,
          leaving: proposing && !!eAct,
          seq: written && eAct ? eAct.seq : undefined,
          seal: eAct ? sealOf([eAct.seq]) : null,
          title: `${nameOf(esc.assigned_member_id)} drives ${m.name} to ${esc.destination} ${say(esc.depart)}–${say(esc.arrive)}${invalid ? " (no longer possible)" : ""}`,
        });
      }
      // who is with the child, in stored state, minute by minute: closes a gap only when it is really covered
      const covered = (iv: string) => {
        const [a, b] = iv.split("-").map(toMin);
        const spans = [
          ...sup.map((x) => [toMin(x.start), toMin(x.end)]),
          ...s.escorts.filter((x) => x.child_id === m.id && x.depart && x.arrive).map((x) => [toMin(x.depart!), toMin(x.arrive!)]),
        ];
        for (let t = a; t < b; t++) if (!spans.some(([p, q]) => p <= t && t < q)) return false;
        return true;
      };
      for (const g of gaps.filter((x) => x.child === m.id)) {
        const [a, b] = g.iv.split("-");
        items.push({
          key: `gap:${m.id}:${g.j}`, row, track: "now", start: a, end: b, kind: "gap", label: "no adult", short: "",
          source: "ingest_school_notice", pin: g.n, closed: written && covered(g.iv),
          seq: written && sAct ? sAct.seq : undefined,
          title: `Conflict ${g.n}: ${m.name} would be home without an adult ${say(a)}–${say(b)}`,
        });
      }
      if (phase === "no-plan") {
        (plan?.unresolved ?? []).forEach((u, i) => {
          if (u.child_id !== m.id || !u.interval) return;
          const [a, b] = u.interval.split("-");
          items.push({ key: `unres:${m.id}:${i}`, row, track: "full", start: a, end: b, kind: "unres", label: `No approved adult ${compact(u.interval)}`, short: "", source: "plan_disruption_recovery", title: ranges(u.detail) });
        });
      }
      // the plan's version of this child's morning, in outline
      if ((proposing || written) && plan) {
        if (sAct) {
          plan.supervision
            .filter((x) => x.child_id === m.id)
            .forEach((seg, i) => {
              const fix = gaps.find((g) => g.child === m.id && toMin(g.iv.split("-")[0]) < toMin(seg.end) && toMin(g.iv.split("-")[1]) > toMin(seg.start));
              items.push({
                key: `ghost:sup:${m.id}:${i}`, row, track: "plan", start: seg.start, end: seg.end, kind: "with", ghost: true,
                settled: written && !!checkOf(sAct.seq)?.verified,
                label: `with ${nameOf(seg.member_id)}`, short: nameOf(seg.member_id), who: seg.member_id, source: "plan_disruption_recovery",
                fix: fix?.n, seq: sAct.seq, title: `Proposed: ${m.name} with ${nameOf(seg.member_id)} ${say(seg.start)}–${say(seg.end)}`,
              });
            });
        }
        for (const asg of plan.assignments.filter((x) => x.child_id === m.id)) {
          const eAct = escortAct(asg.escort_id);
          if (!eAct || !asg.depart || !asg.arrive) continue;
          items.push({
            key: `ghost:ride:${asg.escort_id}`, row, track: "plan", start: asg.depart, end: asg.arrive, kind: "ride", ghost: true,
            settled: written && !!checkOf(eAct.seq)?.verified,
            label: `${nameOf(asg.member_id)} drives`, short: nameOf(asg.member_id), who: asg.member_id, source: "plan_disruption_recovery",
            fix: clashes.get(asg.escort_id)?.n, seq: eAct.seq,
            title: `Proposed: ${nameOf(asg.member_id)} drives ${m.name} ${say(asg.depart)}–${say(asg.arrive)}`,
          });
        }
      }
    }
  }

  // the plan's new calendar entries, in outline on each adult's lane
  for (const a of acts) {
    if (a.type !== "create_calendar_event") continue;
    const member = String(aft(a, "member_id") ?? "");
    if (!rowOf.has(member)) continue;
    const title = String(aft(a, "title") ?? "");
    const resp = (aft(a, "responsibility_id") as string | null) ?? null;
    const start = String(aft(a, "start") ?? "");
    const end = String(aft(a, "end") ?? "");
    if (!start || !end) continue;
    items.push({
      key: `ghost:${eventKey({ id: String(tgt(a, "event_id")), kind: String(aft(a, "kind") ?? ""), responsibility_id: resp })}`,
      row: rowOf.get(member)!, track: "plan", start, end, kind: "duty", ghost: true, settled: written && !!checkOf(a.seq)?.verified,
      label: title, short: shortTitle(title), who: member, source: "plan_disruption_recovery",
      fix: resp ? clashes.get(resp)?.n : undefined, seq: a.seq,
      title: `Proposed: ${nameOf(member)}: ${title} ${say(start)}–${say(end)}`,
    });
  }

  // how the plan connects people: a drive that changes hands, a child handed from one adult to the next
  const links: Link[] = [];
  if (proposing && plan) {
    for (const c of acts.filter((a) => a.type === "cancel_calendar_event")) {
      const resp = bef(c, "responsibility_id") as string | undefined;
      const next = acts.find((a) => a.type === "create_calendar_event" && aft(a, "responsibility_id") === resp);
      if (!resp || !next || !resp.startsWith("r-")) continue;
      const from = eventKey({ id: String(tgt(c, "event_id")), kind: String(bef(c, "kind") ?? ""), responsibility_id: resp });
      const to = `ghost:${eventKey({ id: String(tgt(next, "event_id")), kind: String(aft(next, "kind") ?? ""), responsibility_id: resp })}`;
      links.push({
        key: `move:${resp}`, kind: "move", from, to, who: String(aft(next, "member_id")),
        title: `${String(bef(c, "title"))}: ${nameOf(String(bef(c, "member_id")))} ${say(String(bef(c, "start")))} → ${nameOf(String(aft(next, "member_id")))} ${say(String(aft(next, "start")))}`,
        label: `${String(bef(c, "title")).split(" ").slice(0, 2).join(" ")} → ${nameOf(String(aft(next, "member_id")))} ${sayShort(String(aft(next, "start")))}`,
      });
    }
    for (const child of members.filter((m) => m.role === "child")) {
      const segs = plan.supervision.filter((x) => x.child_id === child.id);
      segs.forEach((seg, i) => {
        const nxt = segs[i + 1];
        if (!nxt || nxt.start !== seg.end || nxt.member_id === seg.member_id) return;
        const adults = [seg.member_id, nxt.member_id];
        const ghostOf = (who: string) =>
          items.find((it) => it.ghost && it.row === rowOf.get(who) && it.end === seg.end) ??
          items.find((it) => it.ghost && it.row === rowOf.get(who) && it.start === seg.end);
        const via = adults.map((w) => ghostOf(w)?.key).filter((k): k is string => !!k);
        links.push({
          key: `handoff:${child.id}:${seg.end}`, kind: "handoff", from: via[0] ?? `ghost:sup:${child.id}:${i}`, to: `ghost:sup:${child.id}:${i + 1}`,
          via, who: nxt.member_id, at: seg.end,
          title: `${say(seg.end)}: ${nameOf(seg.member_id)} hands ${child.name} to ${nameOf(nxt.member_id)}`,
        });
      });
    }
  }

  // the school's time on the axis; once a notice is read, where it was and how far it moved
  const delay = view.notice?.structured.delay_minutes ?? null;
  const flags = s.escorts
    .filter((e) => /school/i.test(e.destination))
    .map((e) => {
      const at = e.window.split("-")[0];
      const from = delay && phase !== "normal" && phase !== "alert" ? hhmm(toMin(at) - delay) : null;
      return { key: `flag:${e.id}`, at, from: from && toMin(from) >= LO ? from : null, delay: from ? delay : null, label: `School ${sayShort(at)}` };
    });

  // DOM order follows the key, never the lane: if React re-ordered a node to follow its new lane,
  // the browser would treat it as newly inserted and the move would jump instead of glide.
  items.sort((p, q) => (p.key < q.key ? -1 : p.key > q.key ? 1 : 0));
  return {
    phase,
    verdict: written ? (verify!.ready ? "ready" : "not-ready") : null,
    mode,
    lanes,
    items,
    links,
    flags,
    readyAt: landingOf(view).readyAt,
  };
}

/** The same picture as words, for screen readers and anyone who cannot see the colours. */
function describe(m: Model): string[] {
  const lines: string[] = [];
  for (const lane of m.lanes) {
    const own = m.items.filter((it) => it.row === lane.row && it.kind !== "window" && it.kind !== "avail");
    const now = own.filter((it) => !it.ghost && !it.closed).map((it) => {
      const tags = [
        it.leaving ? "changes in the plan" : "",
        it.blocker ? "blocks a plan" : "",
        it.seal === true ? "read back" : it.seal === false ? "not read back" : "",
      ].filter(Boolean);
      return `${it.title}${tags.length ? ` (${tags.join(", ")})` : ""}`;
    });
    const plan = own.filter((it) => it.ghost && !it.settled).map((it) => `${it.title}${it.fix ? ` (fixes conflict ${it.fix})` : ""}`);
    const msg = lane.msg ? [lane.msg.summary] : [];
    lines.push(`${lane.name}, ${lane.sub}: ${[...now, ...plan, ...msg].join("; ") || "nothing scheduled"}.`);
  }
  for (const l of m.links) lines.push(`${l.title}.`);
  return lines;
}

const STATUS: Record<Phase, string> = {
  normal: "Current plan",
  alert: "Alert received, not checked yet",
  conflict: "Conflicts found",
  proposed: "Proposed · nothing written yet",
  "no-plan": "No plan covers everyone · nothing was written",
  "set-aside": "Plan set aside · nothing was written",
  written: "Read back from storage",
};

const CAPTION: Record<Phase, string> = {
  normal: "Today's morning",
  alert: "Today's morning",
  conflict: "The morning after the school change",
  proposed: "The morning: now, and with the plan",
  "no-plan": "The morning after the school change",
  "set-aside": "The morning after the school change",
  written: "Household after the change",
};

function Legend({ m }: { m: Model }) {
  const open = m.items.some((it) => (it.kind === "gap" || it.kind === "clash") && !it.closed);
  return (
    <ul className="legend" aria-hidden>
      <li>
        <i className="k care" />
        colour = who is with a child
      </li>
      <li>
        <i className="k busy" />
        fixed
      </li>
      <li>
        <i className="k soft" />
        can move
      </li>
      {open && (
        <li>
          <i className="k gap" />
          no adult
        </li>
      )}
      {m.phase === "proposed" && (
        <li>
          <i className="k ghost" />
          outline = proposed
        </li>
      )}
      {m.mode === "split" && (
        <li>
          <i className="k now" />
          thin bar = now
        </li>
      )}
      {m.phase === "proposed" && m.items.some((it) => it.fix) && (
        <li>
          <i className="k fix">→1</i>
          would fix conflict 1
        </li>
      )}
      {m.phase === "written" && (
        <li>
          <i className="k seal">✓</i>
          read back
        </li>
      )}
    </ul>
  );
}

function Envelope() {
  return (
    <svg viewBox="0 0 16 12" aria-hidden>
      <rect x="0.75" y="0.75" width="14.5" height="10.5" rx="2" />
      <path d="M1.5 1.8 8 6.6l6.5-4.8" />
    </svg>
  );
}

export function LivingTimeline({ view, busy = false }: { view: View; busy?: boolean }) {
  const m = build(view);
  const density = useDensity();
  const plot = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  // the lane each mark was drawn in last time: only a mark that changes lane waits for its plan step
  // before it travels; everything else moves with the rows
  const lastRow = useRef(new Map<string, number>());
  useEffect(() => {
    if (m) lastRow.current = new Map(m.items.map((it) => [it.key, it.row]));
  });
  useEffect(() => {
    const el = plot.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([e]) => setWidth(Math.round(e.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  if (!m) return null;
  const G = GEO[density];
  const H = G[m.mode];
  const plotH = m.lanes.length * H;
  const written = m.phase === "written";
  const geo = new Map(
    m.items.map((it) => {
      const { y, h } = band(density, m.mode, it.row, it.track);
      const x = pct(it.start);
      const w = Math.max(pct(it.end) - x, 0.9);
      return [it.key, { x, w, y, h }] as const;
    }),
  );
  const { fresh } = landingOf(view);
  const delayOf = (it: Item) => (fresh && it.seq ? (it.seq - 1) * STEP_MS : 0);
  const changedLane = (it: Item) => {
    const before = lastRow.current.get(it.key);
    return before !== undefined && before !== it.row;
  };
  const ghosts = m.items.filter((it) => it.ghost).sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
  const arrival = new Map(ghosts.map((g, i) => [g.key, 950 + i * 70]));
  const ticks: number[] = [];
  for (let t = LO; t < HI; t += 30) ticks.push(t);
  // a tick's words that the school flag or its "was"/"+2 h" labels would cover are hidden (the tick line stays)
  const pxPerMin = Math.max(width, 480) / (HI - LO);
  const covered = (t: number) =>
    m.flags.some((f) => {
      const x = (t - LO) * pxPerMin;
      const a = (toMin(f.at) - LO) * pxPerMin;
      if (x + 26 > a && x - 26 < a + f.label.length * 6.6 + 16) return true;
      if (!f.from) return false;
      const from = (toMin(f.from) - LO) * pxPerMin;
      return (x + 26 > from - 4 && x - 26 < from + 58) || Math.abs(x - (from + a) / 2) < 44;
    });
  const lines = describe(m);
  const verify = view.verify;
  const summaryId = `lt-sum-${view.session_id}`;

  const px = (key: string) => {
    const g = geo.get(key);
    return g && width ? { x0: (g.x / 100) * width, x1: ((g.x + g.w) / 100) * width, y: g.y, h: g.h } : null;
  };
  // a drive too short for its words gets them beside it, on the free side of its lane
  const isDrive = (it: Item) => (it.kind === "duty" && it.label.startsWith("Drive ")) || it.kind === "ride";
  // (only where the lane is free up to the next mark; the shorter words if the full ones do not fit)
  const outside = m.items.flatMap((it) => {
    if (it.kind !== "duty" || !isDrive(it) || it.leaving || it.closed || (it.ghost && it.settled) || !width) return [];
    const g = geo.get(it.key)!;
    if ((g.w / 100) * width >= (it.short ?? "").length * 7 + 46) return [];
    const next = m.items
      .filter((o) => o !== it && o.row === it.row && o.track !== "full" && (o.track === it.track || it.track === "full") && !o.closed && !(o.ghost && o.settled) && toMin(o.start) >= toMin(it.end) - 1)
      .reduce((lo, o) => Math.min(lo, pct(o.start)), 100);
    const free = ((next - (g.x + g.w)) / 100) * width - (it.seal != null ? 12 : 6);
    const time = sayShort(it.start);
    const words = [`${it.label.split(" ").slice(0, 2).join(" ")} ${time}`, `${it.short} ${time}`].find((w) => w.length * 6.6 + 18 <= free);
    return words ? [{ it, words }] : [];
  });

  return (
    <figure
      className="lt"
      data-phase={m.phase}
      data-verdict={m.verdict ?? undefined}
      data-mode={m.mode}
      aria-busy={busy || undefined}
      aria-labelledby={`${summaryId}-cap`}
      aria-describedby={summaryId}
      data-density={density}
      style={
        {
          "--row-h": `${H}px`,
          "--plot-h": `${plotH}px`,
          "--plan-top": `${G.planTop - 2}px`,
          "--now-mid": `${G.nowTop + G.nowH / 2}px`,
          "--plan-mid": `${G.planTop + G.track / 2}px`,
          "--ready-at": `${m.readyAt}ms`,
        } as CSSProperties
      }
    >
      <figcaption className="lt-head">
        <span className="tl-title" id={`${summaryId}-cap`}>
          {CAPTION[m.phase]}
          <span className="tag">{STATUS[m.phase]}</span>
        </span>
        {m.verdict && verify && (
          <span className={`lt-verdict ${m.verdict}`} role="status">
            <b aria-hidden>{m.verdict === "ready" ? "✓" : "!"}</b>
            {m.verdict === "ready" ? "Household ready" : "Not ready"}
            <span className="lt-count">
              {verify.writes_verified}/{verify.writes_total} read back
            </span>
          </span>
        )}
      </figcaption>
      <p className="tl-hint">Scroll sideways for the whole morning, 7 AM to 10:45 AM.</p>

      <div className="sr" id={summaryId}>
        <p>
          {STATUS[m.phase]}.
          {m.verdict === "ready" && verify ? ` Household ready: ${verify.writes_verified} of ${verify.writes_total} changes read back.` : ""}
          {m.verdict === "not-ready" && verify
            ? ` Not ready: ${verify.writes_verified} of ${verify.writes_total} changes read back; failed: ${verify.failed_actions.join(", ") || verify.violations.join("; ")}.`
            : ""}
        </p>
        <ul>
          {lines.map((l, i) => (
            <li key={i}>{l}</li>
          ))}
        </ul>
      </div>

      <div className="lt-scroll" aria-hidden>
        <div className="lt-grid">
          <div className="lt-corner" />
          <div className="lt-axis">
            {ticks.map((t) => (
              <span key={t} className={`lt-tick${covered(t) ? " covered" : ""}`} style={{ left: `${((t - LO) / (HI - LO)) * 100}%` }}>
                {say(hhmm(t)).replace(":00", "")}
              </span>
            ))}
            {m.flags.map((f) => (
              <span key={f.key} className="lt-flagset">
                {f.from && (
                  <span
                    key={`trail:${f.from}`}
                    className="lt-trail"
                    data-source="ingest_school_notice"
                    style={{ left: `${pct(f.from)}%`, width: `${pct(f.at) - pct(f.from)}%` }}
                  >
                    <span className="was">was {sayShort(f.from)}</span>
                    <span className="by">
                      +{f.delay && f.delay % 60 === 0 ? `${f.delay / 60} h` : `${f.delay} min`}
                    </span>
                  </span>
                )}
                <span
                  className="lt-flag"
                  data-key={f.key}
                  style={{ "--to": `${pct(f.at)}%`, "--from": `${f.from ? pct(f.from) : pct(f.at)}%` } as CSSProperties}
                >
                  {f.label}
                </span>
              </span>
            ))}
          </div>

          <div className="lt-labels">
            {m.lanes.map((lane) => (
              <div key={lane.id} className={`lt-label ${personClass(lane.id)}`} style={{ "--row": lane.row } as CSSProperties}>
                <Avatar member={{ id: lane.id, name: lane.name }} />
                <span className="who">
                  <span className="line1">
                    <strong>{lane.name}</strong>
                    {lane.msg && (
                      <span
                        key={`msg:${lane.msg.state}`}
                        className={`lt-msg ${lane.msg.state}`}
                        data-seq={lane.msg.seq}
                        data-source={lane.msg.state === "planned" ? "plan_disruption_recovery" : "verify_recovery"}
                        data-verified={lane.msg.state === "planned" ? undefined : String(lane.msg.state === "ok")}
                        title={lane.msg.summary}
                        style={{ "--d": `${written ? (lane.msg.seq - 1) * STEP_MS : 0}ms` } as CSSProperties}
                      >
                        <Envelope />
                        {lane.msg.state === "ok" && <b>✓</b>}
                        {lane.msg.state === "failed" && <b>not sent</b>}
                      </span>
                    )}
                  </span>
                  <small>{lane.sub}</small>
                </span>
              </div>
            ))}
          </div>

          <div className="lt-plot" ref={plot}>
            {m.lanes.map((lane) => (
              <div key={lane.id} className={`lt-row ${personClass(lane.id)}`} style={{ "--row": lane.row } as CSSProperties} />
            ))}
            {m.mode === "split" && (
              <span className="lt-tracknames">
                <span>now</span>
                <span>plan</span>
              </span>
            )}

            {m.items.map((it) => {
              const g = geo.get(it.key)!;
              const runsOn = toMin(it.end) > HI;
              const cls = [
                "lt-item",
                `k-${it.kind}`,
                personClass(it.who),
                it.ghost && "ghost",
                it.settled && "settled",
                it.leaving && "leaving",
                it.invalid && "invalid",
                it.blocker && "blocker",
                it.closed && "closed",
                it.fresh && "fresh",
                runsOn && "runs-on",
                m.mode === "split" && it.track === "now" && "on-now",
              ]
                .filter(Boolean)
                .join(" ");
              return (
                <div
                  key={it.key}
                  className={cls}
                  data-key={it.key}
                  data-source={it.source}
                  data-seq={it.seq}
                  data-person={it.who ?? undefined}
                  data-verified={it.seal == null ? undefined : String(it.seal)}
                  title={it.title}
                  style={
                    {
                      "--x": g.x,
                      "--w": g.w,
                      "--y": `${g.y}px`,
                      "--h": `${g.h}px`,
                      "--d": `${delayOf(it)}ms`,
                      "--dy": `${changedLane(it) ? delayOf(it) : 0}ms`,
                      "--a": `${arrival.get(it.key) ?? 0}ms`,
                    } as CSSProperties
                  }
                >
                  {isDrive(it) && <CarGlyph />}
                  {it.kind === "window" && <PlaceGlyph />}
                  {it.label && <span className="full">{it.label}</span>}
                  {it.short !== undefined && <span className="short">{it.short}</span>}
                  {it.blocker && <span className="hard">hard</span>}
                </div>
              );
            })}

            {outside.map(({ it, words }) => {
              const g = geo.get(it.key)!;
              return (
                <span
                  key={`out:${it.key}`}
                  className={`lt-out ${personClass(it.who)}${it.ghost ? " ghost" : ""}${it.seal != null ? " sealed" : ""}`}
                  data-key={it.key}
                  data-source={it.source}
                  data-seq={it.seq}
                  style={{ "--x": g.x + g.w, "--y": `${g.y}px`, "--h": `${g.h}px`, "--d": `${delayOf(it)}ms`, "--a": `${arrival.get(it.key) ?? 0}ms` } as CSSProperties}
                >
                  {words}
                </span>
              );
            })}

            {width > 0 && m.links.length > 0 &&
              (["under", "over"] as const).map((layer) => (
              <svg key={layer} className={`lt-links ${layer}`} width={width} height={plotH} viewBox={`0 0 ${width} ${plotH}`}>
                {layer === "under" && (
                  <defs>
                    <marker id="lt-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                      <path d="M0 0 8 4 0 8z" fill="context-stroke" />
                    </marker>
                  </defs>
                )}
                {m.links.map((l, i) => {
                  if ((l.kind === "move") !== (layer === "under")) return null;
                  if (l.kind === "move") {
                    const a = px(l.from);
                    const b = px(l.to);
                    if (!a || !b) return null;
                    const down = b.y > a.y;
                    const ax = (a.x0 + a.x1) / 2;
                    const bx = (b.x0 + b.x1) / 2;
                    const ay = down ? a.y + a.h : a.y;
                    const by = down ? b.y - 1 : b.y + b.h + 1;
                    // run along the line between lanes (where no words are), then turn into the new lane
                    const bRow = Math.floor(b.y / H);
                    const my = down ? bRow * H : (bRow + 1) * H;
                    const run = Math.abs(bx - ax);
                    const r = Math.min(9, run / 2, Math.abs(my - ay), Math.abs(by - my));
                    const sx = bx >= ax ? 1 : -1;
                    const sy = down ? 1 : -1;
                    const d =
                      run < 4
                        ? `M${ax} ${ay} L${bx} ${by}`
                        : `M${ax} ${ay} L${ax} ${my - r * sy} Q${ax} ${my} ${ax + r * sx} ${my} L${bx - r * sx} ${my} Q${bx} ${my} ${bx} ${my + r * sy} L${bx} ${by}`;
                    const labelled = !!l.label && run >= 150;
                    return (
                      <g key={l.key} className={`lt-link move ${personClass(l.who)}`} data-key={l.key} style={{ "--i": i } as CSSProperties}>
                        <title>{l.title}</title>
                        <path className="halo" d={d} />
                        <path className="line" d={d} pathLength={1} markerEnd="url(#lt-arrow)" />
                        {labelled && (
                          <text className="lt-link-label" x={bx - (r + 10) * sx} y={my - 4} textAnchor={sx > 0 ? "end" : "start"}>
                            {l.label}
                          </text>
                        )}
                      </g>
                    );
                  }
                  const pts = (l.via ?? []).map(px).filter((p): p is NonNullable<ReturnType<typeof px>> => !!p);
                  const child = px(l.to);
                  if (!l.at || !child) return null;
                  const x = (pct(l.at) / 100) * width;
                  const ys = [...pts.map((p) => p.y + p.h / 2), child.y + child.h / 2];
                  const top = Math.min(...ys);
                  const bottom = Math.max(...ys);
                  return (
                    <g key={l.key} className={`lt-link handoff ${personClass(l.who)}`} data-key={l.key} style={{ "--i": i } as CSSProperties}>
                      <title>{l.title}</title>
                      <line className="halo" x1={x} x2={x} y1={top} y2={bottom} />
                      <line className="line" x1={x} x2={x} y1={top} y2={bottom} />
                      {ys.map((y, j) => (
                        <circle key={j} className="node" cx={x} cy={y} r={4} />
                      ))}
                      <text className="lt-link-label" x={x + 8} y={top + 4}>
                        hands over {sayShort(l.at)}
                      </text>
                    </g>
                  );
                })}
              </svg>
              ))}

            {m.items
              .filter((it) => it.pin || it.fix)
              .map((it) => {
                const g = geo.get(it.key)!;
                const n = it.pin ?? it.fix!;
                return (
                  <span
                    key={`pin:${it.key}`}
                    className={`lt-pin ${it.pin ? "conflict" : "fixes"} ${it.closed || it.settled ? "closed" : ""}`}
                    data-key={it.key}
                    style={{ "--x": g.x, "--y": `${g.y}px`, "--d": `${delayOf(it)}ms`, "--a": `${arrival.get(it.key) ?? 0}ms` } as CSSProperties}
                  >
                    {it.pin ? n : <>→{n}</>}
                  </span>
                );
              })}

            {m.items
              .filter((it) => it.seal != null && !it.ghost)
              .map((it) => {
                const g = geo.get(it.key)!;
                return (
                  <span
                    key={`seal:${it.key}:${it.seal}`}
                    className={`lt-seal ${it.seal ? "ok" : "bad"}`}
                    data-key={it.key}
                    data-seq={it.seq}
                    data-source="verify_recovery"
                    data-verified={String(it.seal)}
                    style={{ "--x": g.x + g.w, "--y": `${g.y}px`, "--d": `${delayOf(it)}ms` } as CSSProperties}
                  >
                    {it.seal ? "✓" : "✗"}
                  </span>
                );
              })}
          </div>
        </div>
      </div>
      <div className="lt-foot">
        <Legend m={m} />
      </div>
    </figure>
  );
}
