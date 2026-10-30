"""Time specs ("00:07", "35%", "random:3-10") and the source -> output timeline map.

The output is a sequence of segments: pieces of the (trimmed, sped-up) source
interleaved with spliced-in ad clips. Subtitles and overlays are authored in
source time and converted to output time through :class:`TimelineMap`.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

MIN_SEGMENT = 0.05  # seconds; shorter source pieces are merged away


class TimeSpecError(ValueError):
    pass


def _parse_point(text: str, duration: float) -> float:
    text = text.strip()
    if not text:
        raise TimeSpecError("Пустое значение времени")
    try:
        if text.endswith("%"):
            return float(text[:-1]) / 100.0 * duration
        if ":" in text:
            seconds = 0.0
            for part in text.split(":"):
                seconds = seconds * 60 + float(part)
            return seconds
        return float(text)
    except ValueError as exc:
        raise TimeSpecError(f"Не понимаю время '{text}'") from exc


def parse_time_spec(spec: str, duration: float, rng: random.Random) -> float:
    """Resolve a time spec against a media duration; result is clamped to [0, duration]."""
    text = spec.strip().lower()
    if text.startswith(("random:", "rand:")):
        body = text.split(":", 1)[1]
        lo, sep, hi = body.partition("-")
        if not sep:
            raise TimeSpecError(f"Ожидался диапазон 'random:A-B', получено '{spec}'")
        a, b = _parse_point(lo, duration), _parse_point(hi, duration)
        value = rng.uniform(min(a, b), max(a, b))
    else:
        value = _parse_point(text, duration)
    return min(max(value, 0.0), duration)


@dataclass(frozen=True)
class Segment:
    kind: str  # "source" | "ad"
    out_start: float
    out_end: float
    src_start: float = 0.0
    src_end: float = 0.0
    speed: float = 1.0
    ad_index: int = -1  # index into the job's clip insertions

    @property
    def out_duration(self) -> float:
        return self.out_end - self.out_start


@dataclass(frozen=True)
class TimelineMap:
    segments: tuple[Segment, ...]

    @property
    def duration(self) -> float:
        return self.segments[-1].out_end if self.segments else 0.0

    def source_segments(self) -> list[Segment]:
        return [s for s in self.segments if s.kind == "source"]

    def to_output(self, t_src: float) -> float | None:
        """Map a source timestamp to output time; None if it was trimmed away."""
        sources = self.source_segments()
        for i, seg in enumerate(sources):
            is_last = i == len(sources) - 1
            if seg.src_start <= t_src < seg.src_end or (is_last and t_src == seg.src_end):
                return seg.out_start + (t_src - seg.src_start) / seg.speed
        return None

    def map_interval(self, a: float, b: float) -> list[tuple[float, float]]:
        """Map a source interval to output pieces (split around spliced ads)."""
        pieces = []
        for seg in self.source_segments():
            lo, hi = max(a, seg.src_start), min(b, seg.src_end)
            if hi - lo > 1e-6:
                pieces.append((
                    seg.out_start + (lo - seg.src_start) / seg.speed,
                    seg.out_start + (hi - seg.src_start) / seg.speed,
                ))
        return pieces

    def containing_segment_end(self, t_out: float) -> float:
        for seg in self.segments:
            if seg.out_start <= t_out < seg.out_end:
                return seg.out_end
        return self.duration


def build_timeline(
    window_start: float,
    window_end: float,
    insertions: list[tuple[float, float]],
    speed: float,
) -> TimelineMap:
    """Build the output timeline.

    ``insertions`` is a list of (source_time, ad_duration). Source time is
    clamped into the kept window; the order of the list defines ``ad_index``.
    """
    if window_end - window_start < MIN_SEGMENT:
        raise TimeSpecError("После обрезки видео слишком короткое")
    order = sorted(range(len(insertions)), key=lambda i: insertions[i][0])

    segments: list[Segment] = []
    cursor_src, cursor_out = window_start, 0.0

    def add_source(until: float) -> None:
        nonlocal cursor_src, cursor_out
        if until - cursor_src < MIN_SEGMENT:
            return
        length = (until - cursor_src) / speed
        segments.append(Segment("source", cursor_out, cursor_out + length,
                                cursor_src, until, speed))
        cursor_src, cursor_out = until, cursor_out + length

    for idx in order:
        at, ad_duration = insertions[idx]
        at = min(max(at, window_start), window_end)
        add_source(at)  # a sub-MIN_SEGMENT remainder is folded into the next piece
        segments.append(Segment("ad", cursor_out, cursor_out + ad_duration, ad_index=idx))
        cursor_out += ad_duration
    add_source(window_end)
    if not any(s.kind == "source" for s in segments):
        raise TimeSpecError("В результате не осталось исходного видео")
    return TimelineMap(tuple(segments))
