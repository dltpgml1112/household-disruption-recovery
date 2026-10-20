"use client";

import { useEffect, useRef, useState } from "react";

import type { Member } from "@/lib/types";

type Recognition = {
  lang: string;
  interimResults: boolean;
  maxAlternatives: number;
  start: () => void;
  stop: () => void;
  onresult: ((e: { results: { 0: { transcript: string } }[] }) => void) | null;
  onend: (() => void) | null;
  onerror: ((e: { error: string }) => void) | null;
};

export function VoiceBar({
  onSend,
  chips,
  busy,
  members,
  speaker,
  onSpeaker,
}: {
  onSend: (text: string, channel: "voice" | "screen") => void;
  chips: string[];
  busy: boolean;
  members: Member[];
  speaker: string;
  onSpeaker: (id: string) => void;
}) {
  const [text, setText] = useState("");
  const [listening, setListening] = useState(false);
  const [supported, setSupported] = useState(false);
  const rec = useRef<Recognition | null>(null);

  useEffect(() => {
    const w = window as unknown as { SpeechRecognition?: new () => Recognition; webkitSpeechRecognition?: new () => Recognition };
    const Ctor = w.SpeechRecognition ?? w.webkitSpeechRecognition;
    if (!Ctor) return;
    const r = new Ctor();
    r.lang = "en-US";
    r.interimResults = false;
    r.maxAlternatives = 1;
    r.onresult = (e) => {
      const said = e.results[0][0].transcript.trim();
      if (said) onSend(said, "voice");
    };
    r.onend = () => setListening(false);
    r.onerror = () => setListening(false);
    rec.current = r;
    setSupported(true);
  }, [onSend]);

  const toggle = () => {
    if (!rec.current) return;
    if (listening) {
      rec.current.stop();
    } else {
      setListening(true);
      rec.current.start();
    }
  };

  return (
    <div className="voicebar">
      <button
        type="button"
        className={`mic ${listening ? "on" : ""}`}
        onClick={toggle}
        disabled={!supported || busy}
        aria-pressed={listening}
        aria-label={listening ? "Stop listening" : "Speak"}
        title={supported ? "Speak (Chrome speech recognition)" : "Speech recognition is not available in this browser"}
      >
        <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden>
          <path
            fill="currentColor"
            d="M12 14a3 3 0 0 0 3-3V5a3 3 0 1 0-6 0v6a3 3 0 0 0 3 3Zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2Z"
          />
        </svg>
      </button>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (text.trim()) {
            onSend(text.trim(), "voice");
            setText("");
          }
        }}
      >
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder='Type what you would say, e.g. "Alexa, fix our morning"'
          aria-label="Utterance"
          disabled={busy}
        />
      </form>
      <label className="speaker">
        Speaking:{" "}
        <select value={speaker} onChange={(e) => onSpeaker(e.target.value)} disabled={busy}>
          {members.map((m) => (
            <option key={m.id} value={m.id}>
              {m.name} ({m.role === "child" ? `child, ${m.age}` : m.role})
            </option>
          ))}
        </select>
      </label>
      <div className="chips" role="group" aria-label="Suggested phrases">
        {chips.map((c) => (
          <button key={c} type="button" onClick={() => onSend(c, "voice")} disabled={busy}>
            {c}
          </button>
        ))}
      </div>
    </div>
  );
}
