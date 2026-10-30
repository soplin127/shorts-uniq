"""Metadata scrubbing, randomized replacement tags and post-encode verification."""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from core.config import ConfigError, MetadataSettings
from core.ffmpeg import FFmpegTools, probe_raw

DEFAULT_DATE_SPAN_DAYS = 365
SECONDS_PER_DAY = 86400

# Tags an ffmpeg-written MP4 legitimately carries; anything else is a leak.
# There is deliberately no "encoder": the mov muxer ignores a custom value and
# always writes its own "Lavf<version>", so the tag is suppressed with +bitexact.
ALLOWED_FORMAT_TAGS = frozenset({
    "major_brand", "minor_version", "compatible_brands", "creation_time", "title", "comment",
})
ALLOWED_STREAM_TAGS = frozenset({"language", "handler_name", "vendor_id", "creation_time"})
# Container-structure keys whose values coincide with the source by design.
STRUCTURAL_KEYS = frozenset({
    "major_brand", "minor_version", "compatible_brands", "language", "handler_name", "vendor_id",
})
MIN_LEAK_VALUE_LEN = 4
# Byte signatures encoders leave inside the bitstream itself (not visible to ffprobe).
TOOL_SIGNATURES = (b"Lavf", b"Lavc", b"x264 - core", b"HandBrake")
SCAN_CHUNK_BYTES = 1 << 20
VIDEO_HANDLER = "VideoHandler"
AUDIO_HANDLER = "SoundHandler"


@dataclass(frozen=True)
class MetadataParams:
    creation_time: str  # 2026-05-12T10:22:33.000000Z
    title: str
    comment: str

    @property
    def date_compact(self) -> str:
        return self.creation_time[:10].replace("-", "")

    @property
    def time_compact(self) -> str:
        return self.creation_time[11:19].replace(":", "")


def _parse_date(text: str, fallback: date) -> date:
    if not text.strip():
        return fallback
    try:
        return date.fromisoformat(text.strip())
    except ValueError as exc:
        raise ConfigError(f"Неверная дата '{text}', ожидается ГГГГ-ММ-ДД") from exc


def date_range(s: MetadataSettings, today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    start = _parse_date(s.date_from, today - timedelta(days=DEFAULT_DATE_SPAN_DAYS))
    end = _parse_date(s.date_to, today)
    if start > end:
        raise ConfigError("Дата 'с' позже даты 'по'")
    return start, end


def roll_metadata(s: MetadataSettings, rng: random.Random, today: date | None = None) -> MetadataParams:
    start, end = date_range(s, today)
    base = datetime.combine(start, time.min, tzinfo=timezone.utc)
    span = int((end - start).total_seconds()) + SECONDS_PER_DAY - 1
    moment = base + timedelta(seconds=rng.randint(0, span))
    title = rng.choice(s.neutral_titles) if s.title_mode == "neutral" and s.neutral_titles else ""
    return MetadataParams(
        creation_time=moment.strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
        title=title,
        comment="",
    )


def metadata_args(p: MetadataParams) -> list[str]:
    """Drop every inherited tag/chapter and write only the new, neutral ones."""
    return [
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-fflags", "+bitexact",  # no "Lavf<version>" encoder tag in the container
        "-flags:a", "+bitexact",  # the AAC encoder embeds "Lavc<version>" in the first frame
        "-metadata", f"creation_time={p.creation_time}",
        "-metadata", f"title={p.title}",
        "-metadata", f"comment={p.comment}",
        "-metadata:s:v:0", "encoder=",  # no "Lavc ... libx264" per-stream tag
        "-metadata:s:a:0", "encoder=",
        "-metadata:s:v:0", f"handler_name={VIDEO_HANDLER}",
        "-metadata:s:a:0", f"handler_name={AUDIO_HANDLER}",
        "-metadata:s:v:0", "language=und",
        "-metadata:s:a:0", "language=und",
    ]


def _source_values(source_tags: dict) -> set[str]:
    """Identifying source values (keys look like 'format.title' or 'stream0.encoder')."""
    return {
        value for key, value in source_tags.items()
        if key.rsplit(".", 1)[-1] not in STRUCTURAL_KEYS and len(value) >= MIN_LEAK_VALUE_LEN
    }


def find_signatures(path: str, signatures: tuple[bytes, ...] = TOOL_SIGNATURES) -> list[str]:
    """Scan the file in chunks for encoder/tool identification strings."""
    overlap = max(len(s) for s in signatures) - 1
    found: set[bytes] = set()
    tail = b""
    with open(path, "rb") as fh:
        while chunk := fh.read(SCAN_CHUNK_BYTES):
            window = tail + chunk
            found.update(s for s in signatures if s in window)
            tail = window[-overlap:]
    return sorted(s.decode() for s in found)


def verify_output(
    tools: FFmpegTools,
    path: str,
    expected: MetadataParams,
    source_tags: dict,
    *,
    min_duration: float = 0.1,
) -> list[str]:
    """Return a list of problems (empty = output is valid and clean)."""
    data = probe_raw(tools, path)
    problems: list[str] = []
    fmt_info = data.get("format") or {}
    streams = data.get("streams") or []

    if float(fmt_info.get("duration") or 0) < min_duration:
        problems.append("длительность результата слишком мала")
    if not any(s.get("codec_type") == "video" for s in streams):
        problems.append("в результате нет видеопотока")
    if data.get("chapters"):
        problems.append("остались главы (chapters)")

    fmt_tags = fmt_info.get("tags") or {}
    for key in fmt_tags:
        if key not in ALLOWED_FORMAT_TAGS:
            problems.append(f"лишний тег контейнера: {key}")
    if fmt_tags.get("creation_time", "")[:19] != expected.creation_time[:19]:
        problems.append("creation_time не совпадает с записанным")

    leaked = _source_values(source_tags) - {expected.title, expected.comment}
    for stream in streams:
        for key, value in (stream.get("tags") or {}).items():
            if key not in ALLOWED_STREAM_TAGS:
                problems.append(f"лишний тег потока {stream.get('index')}: {key}")
            elif key not in STRUCTURAL_KEYS and value in leaked:
                problems.append(f"тег потока {key} совпадает с исходником")
    for key, value in fmt_tags.items():
        if key not in STRUCTURAL_KEYS and value in leaked:
            problems.append(f"тег {key} совпадает с исходником")
    for signature in find_signatures(path):
        problems.append(f"в файле осталась подпись энкодера: {signature}")
    return problems
