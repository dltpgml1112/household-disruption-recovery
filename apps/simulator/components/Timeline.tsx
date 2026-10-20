import { say, toMin } from "@/lib/time";
import type { CalEvent, Conflict, Escort, Member, Segment, Assignment } from "@/lib/types";

export type Tone = "commit" | "soft" | "duty" | "new" | "cancelled" | "with" | "drive" | "gap" | "invalid" | "window";

export interface Bar {
  start: string;
  end: string;
  label: string;
  short?: string; // shown instead of the label when the bar is too narrow for it
  tone: Tone;
}

/** A one-word label for short calendar entries: "Drive", "Emma" (for "With Emma at home"), "Client". */
function shortTitle(title: string): string {
  if (title.startsWith("Drive ")) return "Drive";
  const withWho = /^With (\w+)/.exec(title);
  if (withWho) return withWho[1];
  return title.split(" ")[0];
}

export interface Lane {
  id: string;
  label: string;
  sub?: string;
  bars: Bar[];
}

const RANGE: [number, number] = [7 * 60, 10 * 60 + 45];

export function Timeline({ lanes, caption }: { lanes: Lane[]; caption?: string }) {
  const [lo, hi] = RANGE;
  const pct = (t: string) => ((Math.min(Math.max(toMin(t), lo), hi) - lo) / (hi - lo)) * 100;
  const ticks: number[] = [];
  for (let t = lo; t <= hi; t += 30) ticks.push(t);
  return (
    <figure className="timeline" aria-label={caption ?? "Morning timeline"}>
      {caption && <figcaption>{caption}</figcaption>}
      <div className="tl-axis" aria-hidden>
        {ticks.map((t) => (
          <span key={t} style={{ left: `${((t - lo) / (hi - lo)) * 100}%` }}>
            {say(`${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`).replace(":00", "")}
          </span>
        ))}
      </div>
      {lanes.map((lane) => (
        <div className="tl-lane" key={lane.id}>
          <div className="tl-label">
            <strong>{lane.label}</strong>
            {lane.sub && <small>{lane.sub}</small>}
          </div>
          <div className="tl-track">
            {lane.bars
              .filter((b) => toMin(b.end) > lo && toMin(b.start) < hi)
              .map((b, i) => (
                <div
                  key={`${b.start}-${b.label}-${i}`}
                  className={`tl-bar tone-${b.tone}`}
                  style={{ left: `${pct(b.start)}%`, width: `${Math.max(pct(b.end) - pct(b.start), 1.2)}%` }}
                  title={`${b.label} · ${say(b.start)}–${say(b.end)}`}
                >
                  <span className="full">{b.label}</span>
                  <span className="short" aria-hidden>{b.short ?? ""}</span>
                </div>
              ))}
          </div>
        </div>
      ))}
    </figure>
  );
}

const nameOf = (members: Member[], id: string | null | undefined) =>
  members.find((m) => m.id === id)?.name ?? id ?? "nobody";

export function adultLanes(members: Member[], events: CalEvent[], showCancelled: boolean): Lane[] {
  return members
    .filter((m) => m.role !== "child")
    .filter((m) => m.role !== "caregiver" || events.some((e) => e.member_id === m.id && e.status === "confirmed"))
    .map((m) => ({
      id: m.id,
      label: m.name,
      sub: m.role === "caregiver" ? (m.available ? `caregiver ${m.available_window ?? ""}` : "unavailable today") : "parent",
      bars: events
        .filter((e) => e.member_id === m.id && (showCancelled || e.status === "confirmed"))
        .map<Bar>((e) => ({
          start: e.start,
          end: e.end,
          label: e.title,
          short: shortTitle(e.title),
          tone:
            e.status === "cancelled" ? "cancelled" : e.new ? "new" : e.kind === "duty" ? "duty" : e.hard ? "commit" : "soft",
        })),
    }));
}

export function childLanes(
  members: Member[],
  segments: Segment[],
  escorts: { child_id: string; member_id: string | null; depart: string | null; arrive: string | null; id: string }[],
  conflicts: Conflict[],
  windows: Escort[] = [],
): Lane[] {
  return members
    .filter((m) => m.role === "child")
    .map((child) => {
      const bars: Bar[] = [];
      for (const w of windows.filter((x) => x.child_id === child.id)) {
        const [a, b] = w.window.split("-");
        bars.push({ start: a, end: b, label: "drop-off window", short: "window", tone: "window" });
      }
      for (const s of segments.filter((x) => x.child_id === child.id)) {
        const who = nameOf(members, s.member_id);
        bars.push({ start: s.start, end: s.end, label: `with ${who}`, short: who, tone: "with" });
      }
      for (const c of conflicts.filter((x) => x.code === "SUPERVISION_GAP" && x.child_id === child.id)) {
        for (const iv of c.intervals) {
          const [a, b] = iv.split("-");
          bars.push({ start: a, end: b, label: "no adult", short: "", tone: "gap" });
        }
      }
      const invalid = new Set(conflicts.filter((c) => c.code === "OWNER_UNAVAILABLE").map((c) => c.responsibility_id));
      for (const e of escorts.filter((x) => x.child_id === child.id)) {
        if (!e.depart || !e.arrive) continue;
        bars.push({
          start: e.depart,
          end: e.arrive,
          label: `${nameOf(members, e.member_id)} drives`,
          short: nameOf(members, e.member_id),
          tone: invalid.has(e.id) ? "invalid" : "drive",
        });
      }
      return { id: child.id, label: child.name, sub: child.age ? `age ${child.age}` : undefined, bars };
    });
}

export const fromAssignments = (a: Assignment[]) =>
  a.map((x) => ({ id: x.escort_id, child_id: x.child_id, member_id: x.member_id, depart: x.depart, arrive: x.arrive }));

export const fromEscorts = (e: Escort[]) =>
  e.map((x) => ({ id: x.id, child_id: x.child_id, member_id: x.assigned_member_id, depart: x.depart, arrive: x.arrive }));
