"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode, type RefObject } from "react";

import { McpAppFrame } from "@/components/McpAppFrame";
import { Avatar, Names, personClass } from "@/components/People";
import { TracePanel, type TraceNote } from "@/components/TracePanel";
import { LivingTimeline, landingOf, phaseOf, type Phase } from "@/components/Timeline";
import { VoiceBar } from "@/components/VoiceBar";
import { HOSTED, UnknownSession, api } from "@/lib/api";
import { clockRange, say, span } from "@/lib/time";
import type { Action, McpApp, Member, View } from "@/lib/types";

/** A new tool result gets a fresh frame, so the view runs its handshake again. */
function appKey(app: McpApp): string {
  const r = app.result.structuredContent as Record<string, unknown>;
  return [app.tool, r.plan_id, r.status, r.ready, r.writes_verified].join(":");
}

/* ---------- what the last turn actually did (all read from the view the server returned) ---------- */

/** The household's state after the last turn: dawn, an alert, a plan on the table,
 *  stuck (no plan / set aside / not ready), or ready (verify_recovery said so). */
type Mood = "dawn" | "alert" | "plan" | "stuck" | "ready";

function moodOf(view: View, phase: Phase): Mood {
  if (view.degraded) return "stuck";
  switch (phase) {
    case "alert":
      return "alert";
    case "conflict":
    case "proposed":
      return "plan";
    case "no-plan":
    case "set-aside":
      return "stuck";
    case "written":
      return view.verify?.ready ? "ready" : "stuck";
    default:
      return view.pending_notice ? "alert" : "dawn";
  }
}

/** The server refused an approval this turn (e.g. the caregiver said yes): from the trace's tool calls. */
function refusalOf(view: View): { error: string; who: Member | null } | null {
  if (view.screen !== "C" || view.plan?.status !== "proposed") return null;
  const call = view.trace?.calls.find((c) => c.tool === "approve_recovery_plan" && !c.ok);
  if (!call) return null;
  return { error: call.error ?? "refused", who: view.state?.members.find((m) => m.id === view.speaker) ?? null };
}

/** One short line per tool call of this turn, with the real counts the call returned. */
function traceNotes(view: View, refusal: ReturnType<typeof refusalOf>): Record<string, TraceNote> {
  const notes: Record<string, TraceNote> = {};
  const t = view.trace;
  if (!t) return notes;
  const called = new Set(t.wire.filter((w) => w.method === "tools/call" && w.tool).map((w) => w.tool as string));
  const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

  if (called.has("ingest_school_notice") && view.notice) {
    const d = view.notice.structured.delay_minutes;
    const n = view.conflicts.length;
    notes.ingest_school_notice = {
      text: `${d ? `school +${d} min · ` : ""}${plural(n, "conflict")} found`,
      tone: n ? "bad" : "ok",
    };
  }
  if (called.has("plan_disruption_recovery") && view.plan) {
    const p = view.plan;
    notes.plan_disruption_recovery = p.feasible
      ? { text: `${plural(p.actions.length, "change")} proposed · nothing written`, tone: "plan" }
      : { text: `no plan covers everyone · nothing written`, tone: "warn" };
  }
  if (called.has("approve_recovery_plan")) {
    if (refusal) {
      notes.approve_recovery_plan = {
        text: `refused: ${refusal.error}${refusal.who ? ` · ${refusal.who.name} is a ${refusal.who.role}` : ""}`,
        tone: "bad",
      };
    } else if (view.plan?.status === "rejected") {
      notes.approve_recovery_plan = { text: "declined by a parent · nothing written", tone: "warn" };
    } else if (view.approval?.decision === "approve") {
      notes.approve_recovery_plan = {
        text: `approved${view.approval.approved_actions ? ` ${plural(view.approval.approved_actions, "change")}` : ""}`,
        tone: "ok",
      };
    }
  }
  if (called.has("apply_recovery_plan") && view.apply) {
    const a = view.apply;
    const parts = [`${a.applied} written`];
    if (a.skipped_already_applied) parts.push(`${a.skipped_already_applied} already in`);
    if (a.failed) parts.push(`${a.failed} failed`);
    notes.apply_recovery_plan = { text: parts.join(" · "), tone: a.failed ? "bad" : "ok" };
  }
  if (called.has("verify_recovery") && view.verify) {
    const v = view.verify;
    notes.verify_recovery = {
      text: `${v.writes_verified}/${v.writes_total} read back · ${v.ready ? "ready" : "not ready"}`,
      tone: v.ready ? "ok" : "bad",
    };
  }
  return notes;
}

/* ---------- progress: five numbered rows, as in a drawing's title block ---------- */

const STEPS = ["Detect", "Plan", "Approve", "Apply", "Verify"] as const;

/** The MCP tool that does each step, as the trace names it. */
const STEP_TOOLS: Record<(typeof STEPS)[number], string> = {
  Detect: "ingest_school_notice",
  Plan: "plan_disruption_recovery",
  Approve: "approve_recovery_plan",
  Apply: "apply_recovery_plan",
  Verify: "verify_recovery",
};

/** How far the server has got. A step is done only when its tool's result is in the view: an alert that has
 *  arrived but not been read (pending_notice only) leaves Detect waiting. */
function stepIndex(v: View): number {
  if (v.verify) return v.verify.ready ? 5 : 4;
  if (v.apply) return 3;
  if (v.plan) return v.plan.feasible ? 2 : 1;
  if (v.notice) return 1;
  return 0;
}

function Steps({ v, refused }: { v: View; refused: boolean }) {
  const done = stepIndex(v);
  const failedVerify = !!v.verify && !v.verify.ready;
  const failedApply = !!v.apply?.failed;
  const stuckPlan = !!v.plan && !v.plan.feasible;
  const setAside = v.plan?.status === "rejected" && !v.apply;
  // nothing has happened yet (no alert): no step is "current", so the eye goes to the device first
  const idle = !v.pending_notice && !v.notice && !v.plan;
  return (
    <ol className="steps" aria-label="Recovery progress">
      {STEPS.map((s, i) => {
        const state =
          s === "Apply" && i < done && failedApply
            ? "failed"
            : i < done
              ? "done"
              : idle
                ? ""
                : i === done && ((s === "Verify" && failedVerify) || (s === "Plan" && stuckPlan))
                  ? "failed"
                  : i === done && s === "Approve" && refused
                    ? "blocked"
                    : i === done && s === "Approve" && setAside
                      ? "aside"
                      : i === done && s === "Detect" && v.pending_notice
                        ? "waiting"
                        : i === done
                          ? "current"
                          : "";
        return (
          <li key={s} className={state} aria-current={i === done && !idle ? "step" : undefined}>
            <i aria-hidden>{String(i + 1).padStart(2, "0")}</i>
            <span>
              {s}
              {s === "Apply" && v.apply && (
                <span className="n">
                  {" "}
                  {v.apply.applied + v.apply.skipped_already_applied}/
                  {v.apply.applied + v.apply.skipped_already_applied + v.apply.failed}
                </span>
              )}
              {s === "Verify" && v.verify && (
                <span className="n">
                  {" "}
                  {v.verify.writes_verified}/{v.verify.writes_total}
                </span>
              )}
            </span>
            <code className="tool">{STEP_TOOLS[s]}</code>
            <span className="st" aria-hidden>
              {state === "done" ? "✓" : state === "failed" ? "✗" : state === "blocked" ? <LockGlyph /> : state === "aside" ? "–" : ""}
            </span>
            {state === "done" && <span className="sr"> (done)</span>}
            {state === "failed" && <span className="sr"> (not done)</span>}
            {state === "blocked" && <span className="sr"> (a parent must approve)</span>}
            {state === "aside" && <span className="sr"> (set aside by a parent)</span>}
            {state === "waiting" && <span className="sr"> (an alert is waiting to be checked)</span>}
          </li>
        );
      })}
    </ol>
  );
}

/* ---------- words ---------- */

/** "Mike: 'Client meeting' (hard) 08:45-09:45" -> "... 8:45 AM–9:45 AM" */
const sayRanges = (text: string) => text.replace(/(\d\d:\d\d)-(\d\d:\d\d)/g, (_, a, b) => clockRange(a, b));
const sayTimes = (text: string) => sayRanges(text).replace(/\b(\d\d:\d\d)\b/g, (t) => say(t));

/** The plan in a few large lines: who drives each child, and who is home with whom.
 *  Taken from the planner's own rationale (top-level lines only), with times spelled out. */
function summaryLines(plan: { rationale: string[] }): string[] {
  const top = plan.rationale.filter((r) => !r.startsWith(" ") && !r.startsWith("Caregiver called in"));
  const drives = top.filter((r) => / drives /.test(r));
  const home = top
    .filter((r) => / at home: /.test(r))
    .sort((a, b) => b.split(",").length - a.split(",").length)
    .slice(0, 1);
  return [...drives, ...home].map(sayTimes);
}

function groupActions(actions: Action[]) {
  return {
    duties: actions.filter((a) => a.type === "assign_escort" || a.type === "set_supervision"),
    calendar: actions.filter((a) => a.type.endsWith("calendar_event")),
    messages: actions.filter((a) => a.type === "notify_member"),
  };
}

/** "2026-10-15" -> "Thursday, October 15" (the household's date, not the viewer's clock). */
function longDate(date: string | undefined): string {
  if (!date || !/^\d{4}-\d\d-\d\d$/.test(date)) return "";
  const d = new Date(`${date}T12:00:00Z`);
  return d.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", timeZone: "UTC" });
}

/** "2026-10-15" -> "Thu, Oct 15" */
function shortDate(date: string | undefined): string {
  if (!date || !/^\d{4}-\d\d-\d\d$/.test(date)) return "";
  const d = new Date(`${date}T12:00:00Z`);
  return d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });
}

type Send = (t: string, c: "voice" | "screen") => void;
type Device = "echo" | "phone";
type Platform = "web" | "mobile";

/** Which device the family uses: the wall display (default) or the phone app.
 *  Kept in the address (?device=phone), so a link opens the same view. */
function useDevice(): [Device, (d: Device) => void] {
  const [device, setDevice] = useState<Device>("echo");
  useEffect(() => {
    if (new URLSearchParams(window.location.search).get("device") === "phone") setDevice("phone");
  }, []);
  const choose = useCallback((d: Device) => {
    setDevice(d);
    const url = new URL(window.location.href);
    if (d === "phone") url.searchParams.set("device", "phone");
    else url.searchParams.delete("device");
    window.history.replaceState(null, "", url.toString());
  }, []);
  return [device, choose];
}

/** What sits beside the reply: an MCP App view (app), or a note drawn here (node). */
type Note = { key: string; kind: string; app?: McpApp; node?: ReactNode };

/** The note beside the reply. A new MCP App card is drawn hidden behind the card it replaces and swaps in
 *  only when its view has rendered the tool result (never an empty card); the old one then goes. */
function PinnedSlot({
  note,
  refused,
  platform,
  onMessage,
}: {
  note: Note;
  refused: boolean;
  platform: Platform;
  onMessage: (t: string) => void;
}) {
  const [readyKey, setReadyKey] = useState<string | null>(null);
  const [shown, setShown] = useState<Note | null>(null);
  const [out, setOut] = useState<Note | null>(null);
  const shownRef = useRef<Note | null>(null);
  const slot = useRef<HTMLDivElement>(null);
  shownRef.current = shown;
  const ready = !note.app || readyKey === note.key;

  const arrive = useCallback((n: Note) => {
    const before = shownRef.current;
    setReadyKey(n.key);
    setOut(before && before.key !== n.key ? before : null);
    setShown(n);
  }, []);

  // a note drawn here (the alert, a summary) is on screen at once
  useEffect(() => {
    if (!note.app) {
      setShown(note);
      setOut(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [note.key]);
  // a view that never reports its size is shown anyway
  useEffect(() => {
    if (ready) return;
    const t = window.setTimeout(() => arrive(note), 1500);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [note.key, ready]);
  useEffect(() => {
    if (!out) return;
    const t = window.setTimeout(() => setOut(null), 320);
    return () => window.clearTimeout(t);
  }, [out]);

  const holding = !ready && shown && shown.key !== note.key ? shown : null;
  const leaving = !holding && out && out.key !== note.key ? out : null;
  const startHeight = (slot.current?.querySelector("iframe") as HTMLIFrameElement | null)?.offsetHeight || 140;
  const items: { n: Note; role: "holding" | "leaving" | "waiting" | "in" }[] = [];
  if (holding) items.push({ n: holding, role: "holding" });
  if (leaving) items.push({ n: leaving, role: "leaving" });
  items.push({ n: note, role: ready ? "in" : "waiting" });

  return (
    <div className="pinned-slot" ref={slot}>
      {items.map(({ n, role }) => {
        const live = role === "in" || role === "waiting";
        return (
          <div
            key={n.key}
            className={`pinned ${n.kind}${live && refused ? " refused" : ""}${role === "in" ? "" : ` ${role}`}`}
            inert={role !== "in" || undefined}
            aria-hidden={role !== "in" || undefined}
          >
            {n.app ? (
              <McpAppFrame
                app={n.app}
                platform={platform}
                onMessage={onMessage}
                onReady={role === "waiting" ? () => arrive(n) : undefined}
                startHeight={startHeight}
              />
            ) : (
              n.node
            )}
          </div>
        );
      })}
    </div>
  );
}

/** The family's colours, as a small mark beside the title. */
function FamilyMark({ members }: { members: Member[] }) {
  if (!members.length) return null;
  return (
    <span className="family-mark" aria-hidden>
      {members.map((m) => (
        <i key={m.id} className={personClass(m.id)} />
      ))}
    </span>
  );
}

function Dots({ members }: { members: Member[] }) {
  return (
    <span className="dots" aria-hidden>
      {members.slice(0, 5).map((m) => (
        <i key={m.id} className={personClass(m.id)} />
      ))}
    </span>
  );
}

/** Hovering (or focusing) one thing lights up what it is linked to: a trace row and the marks its
 *  tool produced (data-source), a read-back check and its bar (data-seq). Pure DOM classes, no re-render. */
function useLinkedHighlight(root: RefObject<HTMLElement | null>, reset: unknown) {
  useEffect(() => {
    const el = root.current;
    if (!el) return;
    let lit: Element[] = [];
    const clear = () => {
      lit.forEach((e) => e.classList.remove("is-lit"));
      lit = [];
      el.removeAttribute("data-lit");
    };
    const selectorFor = (target: Element | null): string | null => {
      if (!target || !el.contains(target)) return null;
      const row = target.closest("[data-tool]");
      if (row) {
        const tool = CSS.escape(row.getAttribute("data-tool") ?? "");
        return `[data-tool="${tool}"], [data-source="${tool}"]`;
      }
      const seqEl = target.closest(".checks [data-seq], .lt-plot [data-seq], .lt-labels [data-seq]");
      if (seqEl) {
        const seq = CSS.escape(seqEl.getAttribute("data-seq") ?? "");
        return `[data-seq="${seq}"], .wire [data-tool="verify_recovery"]`;
      }
      return null;
    };
    const over = (e: Event) => {
      const sel = selectorFor(e.target as Element | null);
      if (!sel) {
        if (lit.length) clear();
        return;
      }
      clear();
      lit = Array.from(el.querySelectorAll(sel));
      if (lit.length < 2) {
        lit = [];
        return;
      }
      lit.forEach((n) => n.classList.add("is-lit"));
      el.setAttribute("data-lit", "");
    };
    el.addEventListener("pointerover", over);
    el.addEventListener("focusin", over);
    el.addEventListener("pointerleave", clear);
    el.addEventListener("focusout", clear);
    return () => {
      el.removeEventListener("pointerover", over);
      el.removeEventListener("focusin", over);
      el.removeEventListener("pointerleave", clear);
      el.removeEventListener("focusout", clear);
      clear();
    };
  }, [root, reset]);
}

/** The phone keeps the newest exchange in view: the thread scrolls so the latest reply starts at the top
 *  (the card follows under it), and follows its content as the card arrives and sizes itself. */
function useFollowThread(thread: RefObject<HTMLDivElement | null>, active: boolean) {
  useEffect(() => {
    const el = thread.current;
    if (!active || !el || typeof ResizeObserver === "undefined") return;
    const anchor = () => {
      const speech = el.querySelector<HTMLElement>(":scope > .speech");
      const max = el.scrollHeight - el.clientHeight;
      el.scrollTop = speech ? Math.min(max, speech.offsetTop - el.offsetTop - 8) : max;
    };
    const ro = new ResizeObserver(anchor);
    Array.from(el.children).forEach((c) => ro.observe(c));
    const mo = new MutationObserver(() => {
      ro.disconnect();
      Array.from(el.children).forEach((c) => ro.observe(c));
      anchor();
    });
    mo.observe(el, { childList: true, subtree: true });
    anchor();
    return () => {
      ro.disconnect();
      mo.disconnect();
    };
  }, [thread, active]);
}

/** On the wall display, with the card beside it, Alexa's reply is shown as its first and last sentence
 *  (the card carries the details; the voice and the phone keep every word). */
function wallLine(speech: string): string {
  const parts = speech.match(/[^.!?]+[.!?]+["”]?(\s+|$)/g)?.map((p) => p.trim()) ?? [speech];
  return parts.length > 3 ? `${parts[0]} ${parts[parts.length - 1]}` : speech;
}

export default function Page() {
  const [view, setView] = useState<View | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [voiceOn, setVoiceOn] = useState(true);
  const [speaker, setSpeaker] = useState("sarah");
  const [pending, setPending] = useState<string | null>(null);
  const [device, setDevice] = useDevice();
  const spoken = useRef<string | null>(null);
  const stage = useRef<HTMLDivElement>(null);
  const screen = useRef<HTMLDivElement>(null);
  const thread = useRef<HTMLDivElement>(null);
  const last = useRef<(() => Promise<View | void>) | null>(null);

  const run = useCallback(async (fn: () => Promise<View | void>) => {
    last.current = fn;
    setBusy(true);
    setErr(null);
    try {
      const v = await fn();
      if (v) setView(v);
    } catch (e) {
      if (e instanceof UnknownSession) {
        // The server restarted (a hosted demo can be restarted between visits): start a new morning.
        setView(await api.createSession());
        setErr("The demo server restarted, so this is a new morning. Please try again.");
      } else {
        // a network error or a server error: say what it means for the household, keep the raw words small
        const raw = e instanceof Error ? e.message : String(e);
        const unreachable = e instanceof TypeError || /^5\d\d\b/.test(raw);
        setErr(unreachable ? `Can't reach the household service. Nothing was changed.|${raw}` : raw);
      }
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    run(() => api.createSession());
  }, [run]);

  useEffect(() => {
    if (!view?.speech || view.speech === spoken.current) return;
    spoken.current = view.speech;
    if (voiceOn && typeof window !== "undefined" && "speechSynthesis" in window) {
      window.speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(view.speech);
      u.lang = "en-US";
      u.rate = 1.05;
      window.speechSynthesis.speak(u);
    }
  }, [view?.speech, voiceOn]);

  useLinkedHighlight(stage, view);
  useFollowThread(thread, device === "phone" && !!view);

  const send = useCallback(
    (text: string, channel: "voice" | "screen") => {
      // one turn at a time: a tap on the card while a chip's turn runs would send a second approval
      if (!view || busy) return;
      setPending(text);
      // narrow screens: the bar and chips sit under the timeline, so bring the screen (reply and card) into view
      const el = screen.current;
      if (el && window.matchMedia?.("(max-width: 760px)").matches) {
        const top = el.getBoundingClientRect().top;
        if (top < -24 || top > window.innerHeight * 0.4) {
          const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
          el.scrollIntoView({ block: "start", behavior: still ? "auto" : "smooth" });
        }
      }
      if (voiceOn && typeof window !== "undefined" && "speechSynthesis" in window) {
        // Say something at once; the model can take several seconds.
        window.speechSynthesis.cancel();
        const ack = new SpeechSynthesisUtterance("One moment.");
        ack.lang = "en-US";
        window.speechSynthesis.speak(ack);
      }
      run(() => api.turn(view.session_id, text, channel, speaker)).finally(() => setPending(null));
    },
    [run, view, speaker, voiceOn, busy],
  );

  const chips = useMemo(() => {
    if (!view) return [];
    if (view.screen === "C" && view.plan?.status === "rejected") return ["Alexa, fix our morning", "What did you do?"];
    if (view.screen === "C" && view.plan?.feasible) return ["Yes, do it", "No, not now"];
    if (view.screen === "C") return ["Linda can come after all", "What did you do?"];
    if (view.screen === "D" && view.verify && !view.verify.ready) return ["Try again", "Are we good?"];
    if (view.screen === "D") return ["Are we good?", "What did you do?"];
    return ["Alexa, fix our morning"];
  }, [view]);

  if (!view) {
    return (
      <main className="app loading">
        <p className={err ? "banner" : "connecting"} role={err ? "alert" : "status"}>
          {err ? `Cannot reach the simulator API at ${api.where()}: ${err}` : "Connecting…"}
        </p>
      </main>
    );
  }

  const members = view.state?.members ?? [];
  const nameOf = (id: string | null | undefined) => members.find((m) => m.id === id)?.name ?? id ?? "";
  const phase = phaseOf(view);
  const mood = moodOf(view, phase);
  const refusal = refusalOf(view);
  const notes = traceNotes(view, refusal);
  const pinned = pinnedNote(view, busy, send, refusal);
  const who = members.find((m) => m.id === (pending ? speaker : view.speaker));
  const light = busy
    ? "working"
    : refusal || (view.verify && !view.verify.ready)
      ? "bad"
      : mood === "ready"
        ? "ok"
        : mood === "alert" || phase === "no-plan"
          ? "warn"
          : phase === "proposed"
            ? "ask"
            : "idle";
  const onCard = (t: string) => send(t, "screen");
  const { readyAt } = landingOf(view);
  const [errText, errRaw] = (err ?? "").split("|");
  // the wall display shows the stored drives under the reply while the alert waits (B) and after the write (D)
  const wallAgenda = (view.screen === "B" && !!view.pending_notice) || (view.screen === "D" && !!view.verify);
  const voiceBar = (
    <VoiceBar
      onSend={send}
      chips={chips}
      busy={busy}
      members={members}
      speaker={speaker}
      onSpeaker={setSpeaker}
      locked={refusal?.who && refusal.who.id === speaker ? refusal.who.id : null}
    />
  );
  const speech = <Speech view={view} members={members} pending={pending} who={who} wall={device === "echo" && !!pinned} />;

  return (
    <main
      className="app"
      data-mood={mood}
      data-phase={phase}
      data-device={device}
      data-pending={pending ? "" : undefined}
      aria-busy={busy || undefined}
      style={{ "--ready-at": `${readyAt}ms` } as CSSProperties}
    >
      {/* what a screen reader hears after each turn: Alexa's reply once (the devices themselves are not live regions) */}
      <p className="sr" role="status" aria-atomic="true">
        {pending
          ? "Working on it."
          : [view.speech, refusal && "Only a parent can approve. Nothing was written."].filter(Boolean).join(" ")}
      </p>
      <header className="topbar">
        <div className="brand">
          <FamilyMark members={members} />
          <div>
            <p className="label">Alexa+ simulation (unofficial) · synthetic family · {view.state?.date}</p>
            <h1>Household Disruption Recovery</h1>
          </div>
        </div>
        <div className="controls">
          <span className="label">Show on</span>
          <div className="mode" role="group" aria-label="Show the household on">
            <button type="button" aria-pressed={device === "echo"} onClick={() => setDevice("echo")}>
              <WallGlyph />
              Echo Show
            </button>
            <button type="button" aria-pressed={device === "phone"} onClick={() => setDevice("phone")}>
              <PhoneGlyph />
              Phone
            </button>
          </div>
        </div>
      </header>

      <div className="stage" ref={stage}>
        <section className="board" aria-label="The household">
          <i className="cross tl" aria-hidden />
          <i className="cross tr" aria-hidden />
          <i className="cross bl" aria-hidden />
          <i className="cross br" aria-hidden />
          {(view.degraded || err) && (
            <div className="banner" role="alert">
              <span>{view.degraded ? "Household service unreachable — nothing was changed." : errText}</span>
              {errRaw && <code>{errRaw}</code>}
              {err && last.current && (
                <button type="button" onClick={() => last.current && run(last.current)} disabled={busy}>
                  Try again
                </button>
              )}
            </div>
          )}

          <div className="board-grid">
            <div className="device-slot">
              {device === "echo" ? (
                /* the kitchen wall display: Alexa's reply, and the card the MCP server sent */
                <div className="device-frame wall" data-light={light} ref={screen}>
                  <i className="cam" aria-hidden />
                  <div className="wall-screen">
                    <div className="screen-top">
                      {view.state?.date && <span className="when">{longDate(view.state.date)}</span>}
                    </div>
                    <div className={`lead ${pinned ? "has-note" : ""}`}>
                      <div className="speech-col">
                        {speech}
                        {pinned && wallAgenda && <Agenda view={view} members={members} />}
                      </div>
                      {pinned ? (
                        <PinnedSlot note={pinned} refused={!!refusal} platform="web" onMessage={onCard} />
                      ) : (
                        <Agenda view={view} members={members} />
                      )}
                    </div>
                  </div>
                  <span className="device-light" aria-hidden />
                </div>
              ) : (
                /* the phone: the same conversation in the Alexa app, the card inline, the input at the bottom */
                <div className="device-frame phone" data-light={light} ref={screen}>
                  <div className="phone-screen">
                    <i className="island" aria-hidden />
                    <div className="phone-head">
                      <span className="app-name">
                        Alexa+<small>simulation</small>
                      </span>
                      {view.state?.date && <span className="when">{shortDate(view.state.date)}</span>}
                    </div>
                    <div className="phone-thread" ref={thread}>
                      <History view={view} pending={pending} />
                      {speech}
                      {pinned ? (
                        <PinnedSlot note={pinned} refused={!!refusal} platform="mobile" onMessage={onCard} />
                      ) : (
                        <Agenda view={view} members={members} />
                      )}
                    </div>
                    {voiceBar}
                  </div>
                  <span className="device-light" aria-hidden />
                </div>
              )}
            </div>

            {/* the notes the drawing carries: where the card came from, progress, the numbered conflicts,
                the plan, what was read back */}
            <aside className="notes" aria-label="What the household service did">
              <DeviceNote view={view} pinned={pinned} device={device} />
              {refusal && <Refusal refusal={refusal} />}
              <Steps v={view} refused={!!refusal} />
              <Headline view={view} phase={phase} />
              {view.screen === "C" && view.plan && <Conflicts view={view} nameOf={nameOf} />}
              {view.screen === "C" && view.plan && !view.plan.feasible && <NoPlan view={view} />}
              {view.screen === "C" && view.plan?.feasible && <PlanDetails view={view} />}
              {view.screen === "D" && view.verify && view.state && <Result view={view} busy={busy} send={send} />}
              {view.screen === "D" && view.verify && <ReadBack view={view} />}
            </aside>

            {/* the family's morning: the same element from A to D, so its marks move from where they were */}
            {view.state && (
              <section className={`card living sheet${view.screen === "C" && view.plan ? " compare" : ""}`} aria-label="The family's morning">
                <LivingTimeline view={view} busy={busy} />
              </section>
            )}

          </div>
        </section>

        {/* the simulator's own controls around the wires: demo events on top, under the hood (always open),
            and what you say to the wall display at the bottom (on the phone it is inside the app) */}
        <div className="side">
          <nav className="demo" aria-label="Demo controls">
            <span className="demo-label label">Demo</span>
            <label className="toggle">
              <input
                type="checkbox"
                checked={voiceOn}
                onChange={(e) => {
                  setVoiceOn(e.target.checked);
                  if (!e.target.checked && typeof window !== "undefined") window.speechSynthesis?.cancel();
                }}
              />{" "}
              voice
            </label>
            <button onClick={() => run(() => api.reset(view.session_id, null))} disabled={busy}>
              Reset morning
            </button>
            <button onClick={() => run(() => api.notice(view.session_id))} disabled={busy || view.screen !== "A"}>
              School alert arrives
            </button>
            <button
              onClick={() => run(async () => api.fault(view.session_id, "notify", "linda"))}
              disabled={busy}
              title="The next message to Linda fails once"
            >
              Fail Linda&apos;s message
            </button>
            <button onClick={() => run(() => api.reset(view.session_id, "linda-unavailable"))} disabled={busy}>
              Reset: Linda unavailable
            </button>
          </nav>
          <TracePanel trace={view.trace} endpoint={`${api.where()} → MCP`} notes={notes} pending={busy} stale={!!err} />
          {device === "echo" && voiceBar}
          {HOSTED && view.agent === "strands" && (
            <p className="hosted">
              Shared demo: your own copy of a made-up family. What you type is sent to Google&apos;s Gemini API, so
              please don&apos;t type personal information.
            </p>
          )}
        </div>
      </div>
    </main>
  );
}

/** What was said this turn, and Alexa's reply (or "Working on it…" while the turn runs).
 *  On the wall display beside a card the reply is shortened to its first and last sentence (wallLine). */
function Speech({
  view,
  members,
  pending,
  who,
  wall,
}: {
  view: View;
  members: Member[];
  pending: string | null;
  who: Member | undefined;
  wall: boolean;
}) {
  const lastUser = view.transcript.filter((t) => t.role === "user").slice(-1)[0];
  const reply = view.speech ?? "Good morning. Everyone's plans for today are set.";
  return (
    <div className="speech">
      {pending ? (
        <>
          <p className="you">
            {who && <Avatar member={who} />}
            <span>
              <span className="sr">{who ? `${who.name} said: ` : "You said: "}</span>“{pending}”
            </span>
          </p>
          <div className="said">
            <VoiceGlyph />
            <p className="alexa working">
              <Dots members={members} />
              Working on it…
            </p>
          </div>
        </>
      ) : (
        <>
          {lastUser && (
            <p className="you">
              {who && <Avatar member={who} />}
              <span>
                <span className="sr">{who ? `${who.name} said: ` : "You said: "}</span>“{lastUser.text}”
              </span>
            </p>
          )}
          <div className="said">
            <VoiceGlyph />
            <p className={`alexa${wall && wallLine(reply) !== reply ? " short" : ""}`}>
              <span className="sr">Alexa: </span>
              <Names text={wall ? wallLine(reply) : reply} members={members} />
            </p>
          </div>
        </>
      )}
    </div>
  );
}

/** The drawing's note on the device: what it is showing and where that came from (all from the view). */
function DeviceNote({ view, pinned, device }: { view: View; pinned: Note | null; device: Device }) {
  if (pinned?.app) {
    return (
      <p className="device-note">
        <b>MCP App</b> <code>{pinned.app.uri}</code> · rendered from <code>{pinned.app.tool}</code>
      </p>
    );
  }
  if (view.screen === "B" && view.pending_notice) {
    return (
      <p className="device-note">
        <b>New alert</b> {view.pending_notice.source} · received {view.pending_notice.received_at.slice(11, 16)} · not checked yet
      </p>
    );
  }
  if (view.screen === "A" && view.state?.escorts.length) {
    return (
      <p className="device-note">
        <b>{device === "phone" ? "Alexa app" : "Wall display"}</b> today&apos;s drives, read with <code>get_household_state</code>
      </p>
    );
  }
  return null;
}

/** The wall display between turns: today's drives from the stored state, one row per child. */
function Agenda({ view, members }: { view: View; members: Member[] }) {
  const escorts = [...(view.state?.escorts ?? [])].filter((e) => e.depart).sort((a, b) => (a.depart! < b.depart! ? -1 : 1));
  if (!escorts.length) return null;
  const find = (id: string | null | undefined) => members.find((m) => m.id === id);
  return (
    <section className="agenda" aria-label="Today's drives">
      <p className="agenda-head">Today</p>
      <ol>
        {escorts.map((e) => {
          const driver = find(e.assigned_member_id);
          const child = find(e.child_id);
          return (
            <li key={e.id} className={personClass(e.assigned_member_id)}>
              <time>{say(e.depart!)}</time>
              {driver && <Avatar member={driver} />}
              <span>
                <b>{driver?.name ?? "Nobody"}</b> takes {child?.name ?? e.child_id} to {e.destination}
              </span>
              {e.arrive && <small>arrives {say(e.arrive)}</small>}
            </li>
          );
        })}
      </ol>
    </section>
  );
}

/** The phone shows the conversation so far: earlier lines, quieter, above the newest exchange. */
function History({ view, pending }: { view: View; pending: string | null }) {
  const t = view.transcript;
  let lastUser = -1;
  let lastAlexa = -1;
  if (!pending) {
    for (let i = t.length - 1; i >= 0 && (lastUser < 0 || lastAlexa < 0); i--) {
      if (lastAlexa < 0 && t[i].role === "assistant") lastAlexa = i;
      if (lastUser < 0 && t[i].role === "user") lastUser = i;
    }
  }
  const earlier = t.filter((_, i) => i !== lastUser && i !== lastAlexa).slice(-6);
  if (!earlier.length) return null;
  return (
    <ol className="history" aria-label="Earlier in this conversation">
      {earlier.map((m, i) => (
        <li key={i} className={m.role === "user" ? "you" : "alexa"}>
          <span className="sr">{m.role === "user" ? "Said: " : "Alexa: "}</span>
          {m.text}
        </li>
      ))}
    </ol>
  );
}

/** One line under the progress rows for the states that have no block of their own. */
function Headline({ view, phase }: { view: View; phase: Phase }) {
  let label = "";
  let tone = "";
  let text: ReactNode = null;
  if (view.screen === "A" && phase === "normal") {
    label = "Morning as planned";
    text = (
      <>
        No alerts. Press <b>School alert arrives</b> to start.
      </>
    );
  } else if (phase === "alert" && view.pending_notice) {
    label = `New alert · ${view.pending_notice.source} · not checked yet`;
    tone = "warn";
    text = <>Say “Alexa, fix our morning”.</>;
  } else if (phase === "set-aside") {
    label = "Plan set aside · nothing was written";
    tone = "warn";
    text = <>Ask again when you are ready.</>;
  } else {
    return null;
  }
  return (
    <div className="headline">
      <p className={`label ${tone}`}>{label}</p>
      {text && <p className="hint">{text}</p>}
    </div>
  );
}

function VoiceGlyph() {
  return (
    <span className="voice-glyph" aria-hidden>
      <svg viewBox="0 0 18 18">
        <rect x="2" y="7" width="2.6" height="4" rx="1.3" />
        <rect x="6.2" y="4" width="2.6" height="10" rx="1.3" />
        <rect x="10.4" y="2" width="2.6" height="14" rx="1.3" />
        <rect x="14.6" y="6" width="2.6" height="6" rx="1.3" />
      </svg>
    </span>
  );
}

function WallGlyph() {
  return (
    <svg viewBox="0 0 16 12" aria-hidden>
      <rect x="1" y="1" width="14" height="8.5" rx="1.5" />
      <path d="M6 11.2h4" />
    </svg>
  );
}

function PhoneGlyph() {
  return (
    <svg viewBox="0 0 16 12" aria-hidden>
      <rect x="5" y="0.75" width="6.5" height="10.5" rx="1.6" />
      <path d="M7.4 9.4h1.7" />
    </svg>
  );
}

/** The server refused an approval: who asked, what the tool answered, and that nothing changed.
 *  A note on the drawing (the device itself only says what Alexa says). */
function Refusal({ refusal }: { refusal: NonNullable<ReturnType<typeof refusalOf>> }) {
  return (
    <p className="refusal">
      <LockGlyph />
      <span>
        <b>Only a parent can approve.</b>{" "}
        {refusal.who ? `${refusal.who.name} is the family's ${refusal.who.role}; ` : ""}
        <code>approve_recovery_plan</code> answered <code>{refusal.error}</code>. Nothing was written.
      </span>
    </p>
  );
}

function LockGlyph() {
  return (
    <svg className="lock" viewBox="0 0 14 16" aria-hidden>
      <rect x="1.5" y="7" width="11" height="8" rx="2" />
      <path d="M4 7V5a3 3 0 0 1 6 0v2" />
    </svg>
  );
}

/** What sits beside Alexa's reply: the alert (B), or the card from the MCP server (C, D).
 *  Without an MCP App view the plan falls back to a summary card drawn here.
 *  The key changes only when new data arrives, so the note pins itself once per result. */
function pinnedNote(
  view: View,
  busy: boolean,
  send: Send,
  refusal: ReturnType<typeof refusalOf>,
): Note | null {
  const app = view.app;
  if (view.screen === "B" && view.pending_notice) {
    return {
      key: `alert:${view.pending_notice.received_at}`,
      kind: "is-alert",
      node: (
        <div className="card alert">
          <p className="kicker">New alert · {view.pending_notice.source}</p>
          <blockquote>{view.pending_notice.raw}</blockquote>
          <p className="muted">Received {view.pending_notice.received_at.slice(11, 16)}. Say “Alexa, fix our morning”.</p>
        </div>
      ),
    };
  }
  if ((view.screen === "C" && view.plan) || (view.screen === "D" && view.verify && view.state)) {
    if (app) {
      // a refused approval remounts the card, so its buttons are live again for a parent
      const key = appKey(app) + (refusal ? ":refused" : "");
      return { key, kind: app.tool === "verify_recovery" ? "is-result" : "is-plan", app };
    }
  }
  const plan = view.plan;
  if (view.screen === "C" && view.state && plan?.feasible) {
    return {
      key: `summary:${plan.plan_id}:${plan.status}${refusal ? ":refused" : ""}`,
      kind: "is-plan",
      node: (
        <div className="card summary">
          <p className="kicker">Proposed fix · plan v{plan.version} · card from the MCP server</p>
          {plan.card ? (
            <>
              <h2 className="big-title">{plan.card.title}</h2>
              {plan.card.fields.map((f, i) => (
                <p key={i} className="support">
                  {f}
                </p>
              ))}
            </>
          ) : (
            <ul className="big">
              {summaryLines(plan).map((l, i) => (
                <li key={i}>{l}</li>
              ))}
            </ul>
          )}
          <div className="approve">
            <button className="primary" disabled={busy || plan.status !== "proposed"} onClick={() => send("Yes, do it", "screen")}>
              Apply {plan.actions.length} changes
            </button>
            <button disabled={busy || plan.status !== "proposed"} onClick={() => send("No, not now", "screen")}>
              Not now
            </button>
            <span className="muted">Nothing changes until a parent approves.</span>
          </div>
        </div>
      ),
    };
  }
  return null;
}

/** No plan covers everyone: what is still open, and what is in the way. Nothing was written. */
function NoPlan({ view }: { view: View }) {
  const plan = view.plan!;
  return (
    <div className="card infeasible" role="alert">
      <h2>No feasible plan · nothing was written</h2>
      <p className="muted">
        The planner tried every assignment. The glowing gap on the morning below is what its closest plan still leaves
        open.
      </p>
      {plan.unresolved.map((u, i) => (
        <div key={i} className="unres">
          <p>
            <strong>{sayRanges(u.detail)}</strong>
          </p>
          <ul>{u.blockers.map((b, j) => <li key={j}>{sayRanges(b)}</li>)}</ul>
        </div>
      ))}
      {!view.app && (
        <>
          <h3>A parent needs to choose</h3>
          <ul>{plan.options.map((o, i) => <li key={i}>{sayRanges(o)}</li>)}</ul>
        </>
      )}
      <p className="nothing">The household is not marked ready.</p>
    </div>
  );
}

/** The numbered conflicts; the same numbers are marked on the morning below. */
function Conflicts({ view, nameOf }: { view: View; nameOf: (id: string | null | undefined) => string }) {
  const plan = view.plan!;
  const members = view.state?.members ?? [];
  const n = plan.conflicts.length;
  return (
    <section className={`conflicts ${n ? "" : "none"}`} aria-label="Conflicts" data-source="ingest_school_notice">
      <h2>
        {n} conflict{n === 1 ? "" : "s"} {view.plan && !view.plan.feasible ? "in the current plan" : "detected"}
      </h2>
      {n > 0 && (
        <ol>
          {plan.conflicts.map((c, i) => (
            <li key={i}>
              <span className="num" aria-hidden>
                {i + 1}
              </span>
              <span>
                <Names
                  members={members}
                  text={
                    c.code === "SUPERVISION_GAP"
                      ? `${nameOf(c.child_id)} would be home without an adult ${c.intervals.map(span).join(" and ")}`
                      : c.code === "OWNER_UNAVAILABLE"
                        ? `${nameOf(c.member_id)} can't take ${nameOf(c.child_id)} at the new school time`
                        : c.detail
                  }
                />
              </span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function PlanDetails({ view }: { view: View }) {
  const plan = view.plan!;
  const groups = groupActions(plan.actions);
  const state =
    plan.status === "proposed" ? "nothing written yet" : plan.status === "rejected" ? "set aside · nothing written" : plan.status;
  return (
    <div className="card plan">
      <p className="label">
        Plan v{plan.version} · {plan.actions.length} changes · {state}
      </p>
      <details>
        <summary>All {plan.actions.length} changes</summary>
        <div className="changes">
          <section>
            <h3>Responsibilities</h3>
            <ul>{groups.duties.map((a) => <li key={a.seq}>{a.summary}</li>)}</ul>
          </section>
          <section>
            <h3>Calendars</h3>
            <ul>{groups.calendar.map((a) => <li key={a.seq}>{a.summary}</li>)}</ul>
          </section>
          <section>
            <h3>Messages</h3>
            <ul>{groups.messages.map((a) => <li key={a.seq}>{a.summary}</li>)}</ul>
          </section>
        </div>
      </details>
      <details>
        <summary>Why this plan</summary>
        <ul className="why">
          {plan.rationale.map((r, i) => (
            <li key={i} className={r.startsWith("  ") ? "alt" : ""}>
              {r.trim()}
            </li>
          ))}
          <li className="muted">
            Deterministic planner compared {plan.combinations_evaluated} options · objective{" "}
            {Object.entries(plan.objective)
              .map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`)
              .join(" · ")}
          </li>
        </ul>
      </details>
    </div>
  );
}

function Result({ view, busy, send }: { view: View; busy: boolean; send: Send }) {
  const v = view.verify!;
  const app = view.app;
  const rows: [string, string, boolean][] = [
    ["writes read back", `${v.writes_verified}/${v.writes_total}`, v.writes_verified === v.writes_total],
    ["responsibilities covered", `${v.affected_resolved}/${v.affected_total}`, v.affected_resolved === v.affected_total],
    ["unassigned", String(v.unassigned.length), !v.unassigned.length],
    ["constraint violations", String(v.violations.length), !v.violations.length],
  ];
  return (
    <div className={`card result ${v.ready ? "ready" : "not-ready"}`} data-source="verify_recovery">
      <h2>
        {v.ready ? "Household ready" : "Not ready"} · read back by <code>verify_recovery</code>
      </h2>
      <dl className="receipt">
        {rows.map(([label, value, fine], k) => (
          <div key={label} className={fine ? "" : "off"} style={{ "--k": k } as CSSProperties}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      {!v.ready && (
        <div className="approve">
          <p>Failed: {v.failed_actions.join(", ") || v.violations.join("; ")}</p>
          {!app && (
            <button className="primary" disabled={busy} onClick={() => send("Try again", "screen")}>
              Retry failed changes
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function ReadBack({ view }: { view: View }) {
  const v = view.verify!;
  return (
    <details className="readback" open>
      <summary>Read-back checks · {v.checks.filter((c) => c.verified).length}/{v.checks.length}</summary>
      <ul className="checks">
        {v.checks.map((c) => (
          <li key={c.seq} className={c.verified ? "ok" : "bad"} data-seq={c.seq} tabIndex={0}>
            <b aria-hidden>{c.verified ? "✓" : "✗"}</b>
            <span>
              {c.summary}
              {!c.verified && <span className="sr"> (not read back)</span>}
            </span>
          </li>
        ))}
      </ul>
    </details>
  );
}
