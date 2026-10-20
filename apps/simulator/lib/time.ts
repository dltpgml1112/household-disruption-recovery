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

export const span = (s: string) => {
  const [a, b] = s.split("-");
  return `${say(a)}–${say(b)}`;
};
