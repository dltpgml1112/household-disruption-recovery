export const toMin = (hhmm: string) => {
  const [h, m] = hhmm.split(":").map(Number);
  return h * 60 + m;
};

export const say = (hhmm: string | null | undefined) => {
  if (!hhmm) return "–";
  const [h, m] = hhmm.split(":").map(Number);
  const suffix = h < 12 ? "AM" : "PM";
  const hour = h % 12 === 0 ? 12 : h % 12;
  return `${hour}:${String(m).padStart(2, "0")} ${suffix}`;
};

/** "08:30","10:00" -> "8:30–10:00"; "08:30","17:00" -> "8:30 AM–5:00 PM" (the MCP card's convention). */
export const clockRange = (a: string, b: string) => {
  if (toMin(a) < 12 * 60 && toMin(b) < 12 * 60) return `${say(a).replace(/ AM$/, "")}–${say(b).replace(/ AM$/, "")}`;
  return `${say(a)}–${say(b)}`;
};

export const span = (s: string) => {
  const [a, b] = s.split("-");
  return clockRange(a, b);
};
