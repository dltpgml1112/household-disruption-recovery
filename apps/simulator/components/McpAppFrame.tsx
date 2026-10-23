"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";

import type { McpApp } from "@/lib/types";

const PROTOCOL = "2026-01-26";

type Rpc = { jsonrpc?: string; id?: number | string; method?: string; params?: Record<string, unknown> };

/** Text of a ui/message content block (a single block or a list of blocks). */
function messageText(params: Record<string, unknown> | undefined): string {
  const content = params?.content as unknown;
  const blocks = (Array.isArray(content) ? content : [content]) as { type?: string; text?: unknown }[];
  const block = blocks.find((b) => b && b.type === "text" && typeof b.text === "string");
  return block ? String(block.text).trim() : "";
}

/** Host side of MCP Apps (spec 2026-01-26): the server's ui:// view runs in a sandboxed frame
 *  with no same-origin access, and talks to this page only through postMessage JSON-RPC.
 *  A tap in the view arrives as ui/message and is sent as what the current speaker said. */
export function McpAppFrame({
  app,
  onMessage,
  onReady,
  startHeight = 140,
  platform = "web",
}: {
  app: McpApp;
  /** the device this card is shown on: a wall display ("web") or the phone app ("mobile") */
  platform?: "web" | "mobile";
  /** false when the page did not take the words (another turn is still running) */
  onMessage: (text: string) => boolean;
  /** called once, when the view has drawn the tool result (its first size report after the result) */
  onReady?: () => void;
  /** the height to hold until the view reports its own (the card it replaces), so nothing jumps */
  startHeight?: number;
}) {
  const frame = useRef<HTMLIFrameElement>(null);
  const [height, setHeight] = useState(startHeight);
  const latest = useRef(app);
  const send = useRef(onMessage);
  const ready = useRef(onReady);
  const resultSent = useRef(false);
  const fired = useRef(false);
  useEffect(() => {
    latest.current = app;
    send.current = onMessage;
    ready.current = onReady;
  });

  // a layout effect, so the listener is in place before the frame's document loads and sends ui/initialize
  // (a passive effect can run after a fast srcdoc frame has already spoken, and that request is lost)
  useLayoutEffect(() => {
    const post = (msg: object) => frame.current?.contentWindow?.postMessage(msg, "*");
    const reply = (id: Rpc["id"], result: object) => post({ jsonrpc: "2.0", id, result });
    const fail = (id: Rpc["id"], code: number, message: string) => post({ jsonrpc: "2.0", id, error: { code, message } });
    const theme = window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";

    function onWindowMessage(event: MessageEvent) {
      if (!frame.current || event.source !== frame.current.contentWindow) return;
      const m = event.data as Rpc;
      if (!m || m.jsonrpc !== "2.0" || typeof m.method !== "string") return;
      switch (m.method) {
        case "ui/initialize": {
          const mobile = platform === "mobile";
          const width = Math.round(frame.current?.getBoundingClientRect().width ?? 0);
          reply(m.id, {
            protocolVersion: PROTOCOL,
            hostInfo: { name: "household-disruption-recovery-simulator", version: "0.1.0" },
            hostCapabilities: {},
            hostContext: {
              theme, displayMode: "inline", availableDisplayModes: ["inline"], platform, locale: "en-US",
              deviceCapabilities: { touch: mobile, hover: !mobile },
              ...(width ? { containerDimensions: { width, maxHeight: 900 } } : {}),
              ...(mobile ? { safeAreaInsets: { top: 0, right: 0, bottom: 0, left: 0 } } : {}),
            },
          });
          return;
        }
        case "ui/notifications/initialized":
          post({ jsonrpc: "2.0", method: "ui/notifications/tool-input", params: { arguments: latest.current.input ?? {} } });
          post({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: latest.current.result });
          resultSent.current = true;
          return;
        case "ui/notifications/size-changed": {
          const h = Number(m.params?.height);
          if (Number.isFinite(h) && h > 0) setHeight(Math.min(900, Math.ceil(h)));
          if (resultSent.current && !fired.current) {
            fired.current = true;
            // one frame later, so the new height is laid out before the card is shown
            requestAnimationFrame(() => ready.current?.());
          }
          return;
        }
        case "ui/message": {
          const text = messageText(m.params);
          if (!text) {
            fail(m.id, -32602, "ui/message needs a text content block");
            return;
          }
          // a tap while another turn runs is not taken: answer with an error, so the card says so and
          // turns its buttons back on instead of showing "Sent" for words nobody received
          if (!send.current(text)) {
            fail(m.id, -32000, "the host is still answering; try again in a moment");
            return;
          }
          reply(m.id, {});
          return;
        }
        default:
          if (m.id !== undefined) fail(m.id, -32601, `Method not found: ${m.method}`);
      }
    }
    window.addEventListener("message", onWindowMessage);
    return () => window.removeEventListener("message", onWindowMessage);
  }, []);

  return (
    <figure className="mcp-app" data-source={app.tool}>
      <figcaption title={`MCP App · ${app.uri} · rendered from ${app.tool}`}>
        <b>MCP App</b> · <code>{app.uri}</code> · rendered from <code>{app.tool}</code>
      </figcaption>
      <div className="pin-frame">
        <iframe
          ref={frame}
          title={app.tool === "verify_recovery" ? "Result card (MCP App)" : "Plan card (MCP App)"}
          sandbox="allow-scripts"
          srcDoc={app.html}
          style={{ height }}
        />
      </div>
    </figure>
  );
}
