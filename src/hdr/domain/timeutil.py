"""Minutes-since-midnight helpers. The planner works on a single household day."""

from __future__ import annotations


def hm(value: str | int | None) -> int | None:
    if value is None or isinstance(value, int):
        return value
    hours, minutes = value.strip().split(":")
    total = int(hours) * 60 + int(minutes)
    if not 0 <= total <= 24 * 60:
        raise ValueError(f"time out of range: {value!r}")
    return total


def fmt(minutes: int | None) -> str | None:
    if minutes is None:
        return None
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def fmt_range(start: int, end: int) -> str:
    return f"{fmt(start)}-{fmt(end)}"


Interval = tuple[int, int]


def merge(intervals: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for start, end in sorted(i for i in intervals if i[0] < i[1]):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def subtract(window: Interval, covered: list[Interval]) -> list[Interval]:
    """Parts of `window` not covered by `covered`."""
    gaps: list[Interval] = []
    cursor = window[0]
    for start, end in merge(covered):
        if end <= cursor:
            continue
        if start >= window[1]:
            break
        if start > cursor:
            gaps.append((cursor, min(start, window[1])))
        cursor = max(cursor, end)
        if cursor >= window[1]:
            break
    if cursor < window[1]:
        gaps.append((cursor, window[1]))
    return gaps
