"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { TracePanel } from "@/components/TracePanel";
import { Timeline, adultLanes, childLanes, fromAssignments, fromEscorts } from "@/components/Timeline";
import { VoiceBar } from "@/components/VoiceBar";
import { HOSTED, UnknownSession, api } from "@/lib/api";
import { say, span } from "@/lib/time";
import type { Action, View } from "@/lib/types";

const STEPS = ["Detect", "Plan", "Approve", "Apply", "Verify"] as const;

function stepIndex(v: View): number {
  if (v.verify) return v.verify.ready ? 5 : 4;
  if (v.apply) return 3;
  if (v.plan) return v.plan.feasible ? 2 : 1;
  if (v.pending_notice || v.notice) return 1;
  return 0;
}

function Steps({ v }: { v: View }) {
  const done = stepIndex(v);
  return (
    <ol className="steps" aria-label="Recovery progress">
      {STEPS.map((s, i) => (
        <li key={s} className={i < done ? "done" : i === done ? "current" : ""}>
          {s}
        </li>
      ))}
    </ol>
  );
}

/** "Mike: 'Client meeting' (hard) 08:45-09:45" -> "... 8:45 AM–9:45 AM" */
const sayRanges = (text: string) => text.replace(/(\d\d:\d\d)-(\d\d:\d\d)/g, (_, a, b) => `${say(a)}–${say(b)}`);
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

export default function Page() {
  const [view, setView] = useState<View | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [voiceOn, setVoiceOn] = useState(true);
  const [speaker, setSpeaker] = useState("sarah");
  const [pending, setPending] = useState<string | null>(null);
  const spoken = useRef<string | null>(null);

  const run = useCallback(async (fn: () => Promise<View | void>) => {
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
        setErr(e instanceof Error ? e.message : String(e));
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

  const send = useCallback(
    (text: string, channel: "voice" | "screen") => {
      if (!view) return;
      setPending(text);
      if (voiceOn && typeof window !== "undefined" && "speechSynthesis" in window) {
        // Say something at once; the model can take several seconds.
        window.speechSynthesis.cancel();
        const ack = new SpeechSynthesisUtterance("One moment.");
        ack.lang = "en-US";
        window.speechSynthesis.speak(ack);
      }
      run(() => api.turn(view.session_id, text, channel, speaker)).finally(() => setPending(null));
    },
    [run, view, speaker, voiceOn],
  );

  const chips = useMemo(() => {
    if (!view) return [];
    if (view.screen === "C" && view.plan?.feasible) return ["Yes, do it", "No, not now"];
    if (view.screen === "C") return ["Linda can come after all", "What did you do?"];
    if (view.screen === "D" && view.verify && !view.verify.ready) return ["Try again", "Are we good?"];
    if (view.screen === "D") return ["Are we good?", "What did you do?"];
    return ["Alexa, fix our morning"];
  }, [view]);

  if (!view) {
    return (
      <main className="app loading">
        <p>{err ? `Cannot reach the simulator API at ${api.where()}: ${err}` : "Connecting…"}</p>
      </main>
    );
  }

  const members = view.state?.members ?? [];
  const nameOf = (id: string | null | undefined) => members.find((m) => m.id === id)?.name ?? id ?? "";

  return (
    <main className="app">
      <header className="topbar">
        <div>
          <h1>Household Disruption Recovery</h1>
          <p className="muted">Alexa+ simulation (unofficial) · synthetic family · {view.state?.date}</p>
          {HOSTED && (
            <p className="muted small">
              Shared demo server: you get your own copy of a made-up family. What you type is sent to Google&apos;s
              Gemini API, so please don&apos;t type personal information.
            </p>
          )}
        </div>
        <nav className="demo" aria-label="Demo controls">
          <span className="muted">Demo</span>
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
          <label className="toggle">
            <input type="checkbox" checked={voiceOn} onChange={(e) => setVoiceOn(e.target.checked)} /> voice
          </label>
        </nav>
      </header>

      <div className="stage">
        <section className="device" aria-live="polite">
          <Steps v={view} />
          {(view.degraded || err) && (
            <div className="banner warn" role="alert">
              {view.degraded ? "Household service unreachable — nothing was changed." : err}
            </div>
          )}

          <div className="speech">
            {pending ? (
              <>
                <p className="you">“{pending}”</p>
                <p className="alexa working" role="status">Working on it…</p>
              </>
            ) : (
              <>
                {view.transcript.filter((t) => t.role === "user").slice(-1).map((t, i) => (
                  <p key={i} className="you">“{t.text}”</p>
                ))}
                <p className="alexa">{view.speech ?? "Good morning. Everyone's plans for today are set."}</p>
              </>
            )}
          </div>

          {view.screen === "A" && <ScreenA view={view} />}
          {view.screen === "B" && <ScreenB view={view} />}
          {view.screen === "C" && <ScreenC view={view} nameOf={nameOf} busy={busy} send={send} />}
          {view.screen === "D" && <ScreenD view={view} busy={busy} send={send} />}

          <VoiceBar onSend={send} chips={chips} busy={busy} members={members} speaker={speaker} onSpeaker={setSpeaker} />
        </section>

        <TracePanel trace={view.trace} endpoint={`${api.where()} → MCP`} />
      </div>
    </main>
  );
}

function ScreenA({ view }: { view: View }) {
  const s = view.state;
  if (!s) return null;
  return (
    <div className="card">
      <h2>Today&apos;s morning</h2>
      <Timeline
        caption="Current plan"
        lanes={[
          ...adultLanes(s.members, s.calendar, false),
          ...childLanes(s.members, s.supervision, fromEscorts(s.escorts), []),
        ]}
      />
    </div>
  );
}

function ScreenB({ view }: { view: View }) {
  const s = view.state;
  return (
    <>
      {view.pending_notice && (
        <div className="card alert">
          <h2>New alert · {view.pending_notice.source}</h2>
          <blockquote>{view.pending_notice.raw}</blockquote>
          <p className="muted">Received {view.pending_notice.received_at.slice(11, 16)}. Say “Alexa, fix our morning”.</p>
        </div>
      )}
      {s && (
        <div className="card">
          <Timeline
            caption="Plan before the change"
            lanes={[
              ...adultLanes(s.members, s.calendar, false),
              ...childLanes(s.members, s.supervision, fromEscorts(s.escorts), view.conflicts),
            ]}
          />
        </div>
      )}
    </>
  );
}

function ScreenC({
  view,
  nameOf,
  busy,
  send,
}: {
  view: View;
  nameOf: (id: string | null | undefined) => string;
  busy: boolean;
  send: (t: string, c: "voice" | "screen") => void;
}) {
  const s = view.state;
  const plan = view.plan;
  if (!s || !plan) return null;
  const groups = groupActions(plan.actions);
  const approve = (
    <div className="approve">
      <button className="primary" disabled={busy || plan.status !== "proposed"} onClick={() => send("Yes, do it", "screen")}>
        Apply {plan.actions.length} changes
      </button>
      <button disabled={busy || plan.status !== "proposed"} onClick={() => send("No, not now", "screen")}>
        Not now
      </button>
      <span className="muted">Nothing changes until a parent approves.</span>
    </div>
  );
  return (
    <>
      {plan.feasible ? (
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
          {approve}
        </div>
      ) : (
        <div className="card infeasible" role="alert">
          <h2>No feasible plan</h2>
          <p className="muted">
            The planner tried every assignment. The gap below is what its closest plan still leaves open; the
            conflicts further down are for the current plan.
          </p>
          {plan.unresolved.map((u, i) => (
            <div key={i}>
              <p>
                <strong>{sayRanges(u.detail)}</strong>
              </p>
              <ul>{u.blockers.map((b, j) => <li key={j}>{sayRanges(b)}</li>)}</ul>
            </div>
          ))}
          <h3>A parent needs to choose</h3>
          <ul>{plan.options.map((o, i) => <li key={i}>{sayRanges(o)}</li>)}</ul>
          <p className="muted">Nothing was written. The household is not marked ready.</p>
        </div>
      )}

      <div className="card conflicts">
        <h2>
          {plan.conflicts.length} conflict{plan.conflicts.length === 1 ? "" : "s"} detected
        </h2>
        <ul>
          {plan.conflicts.map((c, i) => (
            <li key={i}>
              {c.code === "SUPERVISION_GAP"
                ? `${nameOf(c.child_id)} would be home without an adult ${c.intervals.map(span).join(" and ")}`
                : c.code === "OWNER_UNAVAILABLE"
                  ? `${nameOf(c.member_id)} can't take ${nameOf(c.child_id)} at the new school time`
                  : c.detail}
            </li>
          ))}
        </ul>
      </div>

      <div className="card compare">
        <Timeline
          caption="Before"
          lanes={[
            ...adultLanes(s.members, s.calendar, false),
            ...childLanes(s.members, s.supervision, fromEscorts(s.escorts), plan.conflicts, s.escorts.filter((e) => e.window.startsWith("10"))),
          ]}
        />
        {plan.feasible && view.proposed_calendar && (
          <Timeline
            caption="After (proposed)"
            lanes={[
              ...adultLanes(s.members, view.proposed_calendar, false),
              ...childLanes(s.members, plan.supervision, fromAssignments(plan.assignments), []),
            ]}
          />
        )}
      </div>

      {plan.feasible && (
        <div className="card plan">
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
      )}
    </>
  );
}

function ScreenD({ view, busy, send }: { view: View; busy: boolean; send: (t: string, c: "voice" | "screen") => void }) {
  const v = view.verify;
  const s = view.state;
  if (!v || !s) return null;
  return (
    <>
      <div className={`card result ${v.ready ? "ready" : "not-ready"}`}>
        <h2>{v.ready ? "HOUSEHOLD READY ✓" : "NOT READY"}</h2>
        <dl className="tiles">
          <div>
            <dt>Writes read back</dt>
            <dd>
              {v.writes_verified}/{v.writes_total}
            </dd>
          </div>
          <div>
            <dt>Responsibilities resolved</dt>
            <dd>
              {v.affected_resolved}/{v.affected_total}
            </dd>
          </div>
          <div>
            <dt>Unassigned</dt>
            <dd>{v.unassigned.length}</dd>
          </div>
          <div>
            <dt>Constraint violations</dt>
            <dd>{v.violations.length}</dd>
          </div>
        </dl>
        {!v.ready && (
          <div className="approve">
            <p>Failed: {v.failed_actions.join(", ") || v.violations.join("; ")}</p>
            <button className="primary" disabled={busy} onClick={() => send("Try again", "screen")}>
              Retry failed changes
            </button>
          </div>
        )}
      </div>
      <div className="card">
        <Timeline
          caption="Household after the change (read back from storage)"
          lanes={[
            ...adultLanes(s.members, s.calendar, false),
            ...childLanes(s.members, s.supervision, fromEscorts(s.escorts), []),
          ]}
        />
        <details>
          <summary>Read-back checks</summary>
          <ul className="checks">
            {v.checks.map((c) => (
              <li key={c.seq} className={c.verified ? "ok" : "bad"}>
                {c.verified ? "✓" : "✗"} {c.summary}
              </li>
            ))}
          </ul>
        </details>
      </div>
    </>
  );
}
