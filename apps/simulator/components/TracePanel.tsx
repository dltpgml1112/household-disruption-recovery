import type { Trace } from "@/lib/types";

export function TracePanel({ trace, endpoint }: { trace: Trace | null; endpoint: string }) {
  return (
    <aside className="trace" aria-label="MCP trace">
      <header>
        <h2>MCP trace</h2>
        <p>
          Real JSON-RPC over Streamable HTTP · last turn
          {trace?.protocol_version && (
            <>
              {" "}· protocol <code>{trace.protocol_version}</code>
            </>
          )}
        </p>
        <p className="muted">
          endpoint <code>{endpoint}</code> · agent <code>{trace?.agent ?? "–"}</code>
          {trace?.model && trace.agent === "strands" && (
            <>
              {" "}· model <code>{trace.model}</code> · {trace.model_calls ?? 0} model calls
            </>
          )}
          {trace && !trace.tools.includes("approve_recovery_plan") && trace.agent === "strands" && (
            <> · {trace.tools.length} of 8 tools given to the model (approve is the orchestrator&apos;s)</>
          )}
          {trace && (trace.tools.includes("approve_recovery_plan") || trace.agent !== "strands") && (
            <> · {trace.tools.length} tools listed</>
          )}
        </p>
        {trace?.fallback && <p className="muted">LLM unavailable this turn ({trace.fallback}); rule agent answered.</p>}
      </header>
      {!trace || trace.wire.length === 0 ? (
        <p className="muted">No calls yet.</p>
      ) : (
        <ol className="wire">
          {trace.wire.map((w, i) => {
            const isTool = w.method === "tools/call";
            const failed = (w.status ?? 0) >= 400;
            return (
              <li key={i} className={`${isTool ? "is-tool" : ""} ${failed ? "is-failed" : ""}`}>
                <span className="t">{w.at_ms ?? 0}ms</span>
                <span className={`code s${String(w.status ?? 0)[0]}`}>{w.status ?? "…"}</span>
                <span className="m">
                  {w.method}
                  {w.tool && <b> {w.tool}</b>}
                  {w.method === "server/discover" && failed && <> (newer protocol refused, client falls back)</>}
                </span>
                <span className="h" title="MCP-Protocol-Version request header">
                  {w.protocol_header ?? "—"}
                </span>
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
    </aside>
  );
}
