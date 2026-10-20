import type { View } from "./types";

// "same-origin" is used when the simulator is served by the hosted server next to /api.
const CONFIGURED = process.env.NEXT_PUBLIC_HDR_API;
const BASE = CONFIGURED === "same-origin" ? "" : (CONFIGURED ?? "http://127.0.0.1:8766");

export const HOSTED = process.env.NEXT_PUBLIC_HDR_HOSTED === "1";

export class UnknownSession extends Error {}

async function call(path: string, body?: unknown): Promise<View> {
  const res = await fetch(`${BASE}${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 404 && path.startsWith("/api/session/")) {
    throw new UnknownSession("This session is no longer on the server.");
  }
  if (!res.ok) {
    throw new Error(`${res.status} ${await res.text()}`);
  }
  return res.json();
}

export const api = {
  base: BASE,
  where: () => BASE || (typeof window !== "undefined" ? window.location.origin : ""),
  createSession: () => call("/api/session", {}),
  turn: (sid: string, utterance: string, channel: "voice" | "screen", speaker?: string) =>
    call(`/api/session/${sid}/turn`, { utterance, channel, speaker }),
  notice: (sid: string) => call(`/api/session/${sid}/notice`, {}),
  reset: (sid: string, variant: string | null) => call(`/api/session/${sid}/reset`, { variant }),
  fault: async (sid: string, target: string, selector: string) => {
    const res = await fetch(`${BASE}/api/session/${sid}/fault`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ target, selector, times: 1 }),
    });
    if (res.status === 404) throw new UnknownSession("This session is no longer on the server.");
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  },
};
