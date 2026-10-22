"use client";

import { useEffect, useRef, type CSSProperties } from "react";

import type { Trace } from "@/lib/types";

/** One line under a tool call: what that call really returned (counts from the view), and its tone. */
export interface TraceNote {
  text: string;
  tone: "ok" | "bad" | "warn" | "plan";
}

/** Who decides what in this turn, in words that match the code (agent_rules.py / agent_strands.py). */
function roleOf(trace: Trace): string {
  if (trace.agent === "strands") return "an LLM picks the tools";
  if (trace.agent === "rules") return "no LLM, a rule parser picks the tools";
  return "";
}

/** Under the hood: the real JSON-RPC rows of the last turn, in the order they happened.
 *  A tools/call row carries data-tool, so hovering it lights the marks its tool produced (data-source).
 *  While a turn is running the rows stay as they were and say they belong to the previous turn. */
export function TracePanel({
  trace,
  endpoint,
  notes = {},
  pending = false,
  stale = false,
}: {
  trace: Trace | null;
  endpoint: string;
  notes?: Record<string, TraceNote>;
  pending?: boolean;
  /** the last request failed, so the rows below are from the turn before it */
  stale?: boolean;
}) {
  const wire = trace?.wire ?? [];
  const list = useRef<HTMLOListElement>(null);
  // the list remounts for a new turn, so its rows arrive once in their real order
  const turnKey = wire.map((w) => `${w.at_ms}:${w.method}:${w.tool ?? ""}:${w.status}`).join("|");
  // a tool can be called twice in one turn (get_household_state): its note goes on the last call
  const lastIndex = new Map<string, number>();
  wire.forEach((w, i) => {
    if (w.method === "tools/call" && w.tool) lastIndex.set(w.tool, i);
  });
  // every request after initialize carried the same MCP-Protocol-Version header: say it once, not on every row
  const after = wire.filter((w) => w.method !== "initialize");
  const uniform = !!trace?.protocol_version && after.length > 0 && after.every((w) => w.protocol_header === trace.protocol_version);

  // each turn, put this turn's results in view (the panel's title and endpoint stay put above the rows):
  // from the first call that carries a result note if all of them fit, else as many as fit ending with the
  // last one; always starting on a row's top edge, never half a row
  useEffect(() => {
    const ol = list.current;
    const box = ol?.closest(".wire-scroll") as HTMLElement | null;
    if (!ol || !box) return;
    const rows = Array.from(ol.querySelectorAll<HTMLElement>(":scope > li"));
    const noted = rows.filter((r) => r.classList.contains("has-note"));
    const top = (r: HTMLElement) => r.offsetTop - ol.offsetTop;
    if (box.scrollHeight <= box.clientHeight + 1 || !noted.length) {
      box.scrollTop = 0;
      return;
    }
    const first = noted[0];
    const lastRow = noted[noted.length - 1];
    const end = top(lastRow) + lastRow.offsetHeight;
    let target = top(first) - 4;
    if (end - target > box.clientHeight) {
      const need = end - box.clientHeight;
      target = top(rows.find((r) => top(r) >= need) ?? lastRow);
    }
    box.scrollTop = Math.max(0, target);
  }, [turnKey]);

  return (
    <aside className="trace" aria-label="MCP trace" aria-busy={pending || undefined}>
      <header>
        <p className="kicker">MCP trace</p>
        <h2>Under the hood</h2>
        <p className="trace-lead">
          Real JSON-RPC over Streamable HTTP, last turn
          {trace?.protocol_version && (
            <>
              {" "}· <code>MCP-Protocol-Version: {trace.protocol_version}</code>
              {uniform && <> on every request after initialize</>}
            </>
          )}
        </p>
        <dl className="meta">
          <dt>endpoint</dt>
          <dd>
            <code>{endpoint}</code>
          </dd>
          <dt>agent</dt>
          <dd>
            <code>{trace?.agent ?? "–"}</code>
            {trace && roleOf(trace) && (
              <span className="role"> · {roleOf(trace)}; the MCP server plans, checks approval and reads back</span>
            )}
          </dd>
          {trace?.model && trace.agent === "strands" && (
            <>
              <dt>model</dt>
              <dd>
                <code>{trace.model}</code> · {trace.model_calls ?? 0} model calls
              </dd>
            </>
          )}
          {trace && (
            <>
              <dt>tools</dt>
              <dd>
                {!trace.tools.includes("approve_recovery_plan") && trace.agent === "strands" ? (
                  <>{trace.tools.length} of 8 tools given to the model (approve is the orchestrator&apos;s)</>
                ) : (
                  <>{trace.tools.length} tools listed</>
                )}
              </dd>
            </>
          )}
        </dl>
        {trace?.fallback && <p className="fallback">LLM unavailable this turn ({trace.fallback}); rule agent answered.</p>}
      </header>
      {pending && (
        <p className="waiting">
          <i aria-hidden />
          This turn is running. The rows below are from the previous turn.
        </p>
      )}
      {!pending && stale && (
        <p className="waiting bad">
          <i aria-hidden />
          The last request failed. The rows below are from the turn before it.
        </p>
      )}
      <div
        className="wire-scroll"
        onScroll={(e) => e.currentTarget.toggleAttribute("data-scrolled", e.currentTarget.scrollTop > 2)}
      >
      {!trace || wire.length === 0 ? (
        <p className="empty">No calls yet.</p>
      ) : (
        <ol key={turnKey} ref={list} className="wire" aria-label="JSON-RPC calls, milliseconds from the start of the turn">
          {wire.map((w, i) => {
            const isTool = w.method === "tools/call" || w.method === "resources/read";
            const failed = (w.status ?? 0) >= 400;
            const note = w.tool && lastIndex.get(w.tool) === i ? notes[w.tool] : undefined;
            const tool = w.method === "tools/call" && w.tool ? w.tool : undefined;
            return (
              <li
                key={i}
                className={[isTool ? "is-tool" : "is-plumbing", failed && "is-failed", note && `has-note tone-${note.tone}`].filter(Boolean).join(" ")}
                data-tool={tool}
                tabIndex={tool ? 0 : undefined}
                style={{ "--i": i } as CSSProperties}
              >
                <span className="t">{w.at_ms ?? 0}ms</span>
                <span className={`code s${String(w.status ?? 0)[0]}`}>{w.status ?? "…"}</span>
                <span className="m">
                  {w.method}
                  {w.tool && <b> {w.tool}</b>}
                  {w.method === "server/discover" && failed && (
                    <span className="why"> (newer protocol refused, client falls back)</span>
                  )}
                </span>
                {note && (
                  <span className="note">
                    <span aria-hidden>↳ </span>
                    {note.text}
                  </span>
                )}
                {!uniform && (
                  <span className="h" title="MCP-Protocol-Version request header">
                    {w.protocol_header ?? "—"}
                  </span>
                )}
              </li>
            );
          })}
        </ol>
      )}
      {trace && trace.calls.some((c) => !c.ok) && (
        <div className="trace-errors">
          {trace.calls
            .filter((c) => !c.ok)
            .map((c, i) => (
              <p key={i}>
                <code>{c.tool}</code> attempt {c.attempt}: {c.error}
              </p>
            ))}
        </div>
      )}
      </div>
    </aside>
  );
}
