"""Subtitles: SRT/ASS parsing, mapping to output time, Shorts-style chunking, ASS rendering."""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

from core.config import SubtitleSettings
from core.timeline import TimelineMap

SIDECAR_EXTENSIONS = (".srt", ".ass")
TEXT_ENCODINGS = ("utf-8-sig", "cp1251", "latin-1")  # cp1251: legacy Russian SRTs on Windows
ALIGNMENT = {"bottom": 2, "center": 5, "top": 8}
SIDE_MARGIN = 60
MAX_GAP_FILL = 0.5  # keep a chunk on screen across pauses shorter than this
MIN_EVENT_SECONDS = 0.25
MIN_WORD_SECONDS = 0.05
SENTENCE_END = (".", "!", "?", "…")

_SRT_TIME = re.compile(
    r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
)
_HTML_TAG = re.compile(r"<[^>]+>")
_ASS_OVERRIDE = re.compile(r"\{[^}]*\}")


class SubtitleError(ValueError):
    pass


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    words: tuple[Word, ...]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


# ------------------------------------------------------------------ reading


def find_sidecar(source: Path) -> Path | None:
    """Same-name .srt/.ass next to the video (extension case ignored)."""
    wanted = {source.stem.lower() + ext for ext in SIDECAR_EXTENSIONS}
    for candidate in sorted(source.parent.iterdir()):
        if candidate.is_file() and candidate.name.lower() in wanted:
            return candidate
    return None


def read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in TEXT_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise SubtitleError(f"Не удалось прочитать кодировку {path}")


def spread_words(start: float, end: float, text: str) -> tuple[Word, ...]:
    """Without word timings, share the cue time between words by character length."""
    tokens = text.split()
    if not tokens:
        return ()
    total = sum(len(t) for t in tokens)
    words, cursor = [], start
    for token in tokens:
        length = (end - start) * len(token) / total
        words.append(Word(cursor, cursor + length, token))
        cursor += length
    return tuple(words)


def _make_cue(start: float, end: float, text: str) -> Cue | None:
    text = " ".join(text.split())
    if end <= start or not text:
        return None
    return Cue(start, end, spread_words(start, end, text))


def parse_srt(text: str) -> list[Cue]:
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
        lines = block.strip().split("\n")
        for i, line in enumerate(lines):
            match = _SRT_TIME.search(line)
            if match:
                g = [int(x) for x in match.groups()]
                start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 10 ** len(match.group(4))
                end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 10 ** len(match.group(8))
                cue = _make_cue(start, end, _HTML_TAG.sub("", " ".join(lines[i + 1:])))
                if cue:
                    cues.append(cue)
                break
    return sorted(cues, key=lambda c: c.start)


def _ass_time(value: str) -> float:
    h, m, s = value.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_ass(text: str) -> list[Cue]:
    cues, fields, in_events = [], None, False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_events = line.lower() == "[events]"
            continue
        if not in_events or ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key == "Format":
            fields = [f.strip().lower() for f in value.split(",")]
        elif key == "Dialogue" and fields:
            parts = value.split(",", len(fields) - 1)
            row = dict(zip(fields, parts))
            body = _ASS_OVERRIDE.sub("", row.get("text", ""))
            body = re.sub(r"\\[Nnh]", " ", body)
            cue = _make_cue(_ass_time(row["start"]), _ass_time(row["end"]), body)
            if cue:
                cues.append(cue)
    return sorted(cues, key=lambda c: c.start)


def load_sidecar(path: Path) -> list[Cue]:
    text = read_text(path)
    return parse_ass(text) if path.suffix.lower() == ".ass" else parse_srt(text)


# ------------------------------------------------------- mapping & chunking


def map_word(word: Word, timeline: TimelineMap) -> Word | None:
    """Source-time word -> output time; dropped if it fell into a trimmed part."""
    end = max(word.end, word.start + MIN_WORD_SECONDS)
    pieces = timeline.map_interval(word.start, end)
    if not pieces:
        return None
    start, stop = pieces[0]
    return Word(start, stop, word.text)


def _mapped_words(cue: Cue, timeline: TimelineMap) -> list[Word]:
    return [w for w in (map_word(w, timeline) for w in cue.words) if w]


def chunk_cue_words(words: list[Word], max_words: int) -> list[Cue]:
    """Split words into chunks of at most ``max_words``, breaking after sentence ends."""
    chunks, current = [], []
    for word in words:
        current.append(word)
        if len(current) >= max_words or word.text.endswith(SENTENCE_END):
            chunks.append(Cue(current[0].start, current[-1].end, tuple(current)))
            current = []
    if current:
        chunks.append(Cue(current[0].start, current[-1].end, tuple(current)))
    return chunks


def build_events(cues: list[Cue], timeline: TimelineMap, settings: SubtitleSettings) -> list[Cue]:
    """Output-time events: Shorts chunks, or whole cues split around spliced ads."""
    events: list[Cue] = []
    for cue in cues:
        words = _mapped_words(cue, timeline)
        if settings.shorts_style:
            events += chunk_cue_words(words, settings.max_words)
            continue
        for start, end in timeline.map_interval(cue.start, cue.end):
            inside = tuple(w for w in words if start - 1e-6 <= w.start < end)
            if inside:
                events.append(Cue(start, end, inside))
    return _settle_timing(sorted(events, key=lambda e: e.start))


def _settle_timing(events: list[Cue]) -> list[Cue]:
    """Remove overlaps, bridge short pauses, enforce a minimum on-screen time."""
    settled = []
    for i, event in enumerate(events):
        next_start = events[i + 1].start if i + 1 < len(events) else None
        end = max(event.end, event.start + MIN_EVENT_SECONDS)
        if next_start is not None:
            if next_start - end < MAX_GAP_FILL:
                end = next_start
            end = min(end, next_start)
        if end - event.start > 1e-3:
            settled.append(replace(event, end=end))
    return settled


# ---------------------------------------------------------------- rendering


def ass_color(color: str) -> str:
    """'#RRGGBB' -> '&H00BBGGRR' (ASS is little-endian BGR with inverted alpha)."""
    value = color.strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        raise SubtitleError(f"Цвет должен быть в формате #RRGGBB: {color}")
    return f"&H00{value[4:6]}{value[2:4]}{value[0:2]}".upper()


def ass_timestamp(seconds: float) -> str:
    cs = int(round(max(seconds, 0.0) * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def srt_timestamp(seconds: float) -> str:
    ms = int(round(max(seconds, 0.0) * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")")


def _display(text: str, settings: SubtitleSettings) -> str:
    return text.upper() if settings.uppercase else text


def ass_header(settings: SubtitleSettings, width: int, height: int) -> str:
    bold = -1 if settings.bold else 0
    style = ",".join(str(v) for v in (
        "Default", settings.font, settings.font_size,
        ass_color(settings.primary_color), ass_color(settings.highlight_color),
        ass_color(settings.outline_color), "&H80000000",
        bold, 0, 0, 0, 100, 100, 0, 0, 1,
        settings.outline, settings.shadow, ALIGNMENT[settings.position],
        SIDE_MARGIN, SIDE_MARGIN, settings.margin_v, 1,
    ))
    return "\n".join([
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: {style}",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ])


def _dialogue(start: float, end: float, text: str) -> str:
    return f"Dialogue: 0,{ass_timestamp(start)},{ass_timestamp(end)},Default,,0,0,0,,{text}"


def _highlight_lines(event: Cue, settings: SubtitleSettings) -> list[str]:
    """One line per word: the current word recoloured, the rest in the primary colour."""
    hl, base = ass_color(settings.highlight_color), ass_color(settings.primary_color)
    words = [_ass_escape(_display(w.text, settings)) for w in event.words]
    lines = []
    for i, word in enumerate(event.words):
        start = event.start if i == 0 else word.start
        end = event.end if i == len(event.words) - 1 else event.words[i + 1].start
        if end - start <= 1e-3:
            continue
        parts = [f"{{\\c{hl}&}}{w}{{\\c{base}&}}" if j == i else w for j, w in enumerate(words)]
        lines.append(_dialogue(start, end, " ".join(parts)))
    return lines


def render_ass(events: list[Cue], settings: SubtitleSettings, width: int, height: int) -> str:
    lines = [ass_header(settings, width, height)]
    for event in events:
        if settings.highlight_current_word and len(event.words) > 1:
            lines += _highlight_lines(event, settings)
        else:
            lines.append(_dialogue(event.start, event.end,
                                   _ass_escape(_display(event.text, settings))))
    return "\n".join(lines) + "\n"


def render_srt(events: list[Cue], settings: SubtitleSettings) -> str:
    blocks = [
        f"{i}\n{srt_timestamp(e.start)} --> {srt_timestamp(e.end)}\n{_display(e.text, settings)}\n"
        for i, e in enumerate(events, 1)
    ]
    return "\n".join(blocks)
