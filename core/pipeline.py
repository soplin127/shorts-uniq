"""Turns one source file + preset into a single ffmpeg command.

Graph layout (one pass, one encode):

    source piece 1 ─ uniq video/audio chains ─┐
    ad clip 1      ─ normalize chains        ─┤ concat ─ overlays ─ subtitles ─ encode
    source piece 2 ─ uniq video/audio chains ─┘            music mix ───────────┘

Each source piece is its own ``-ss/-t`` input, so no split/buffering is needed.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path

from core.config import Preset
from core.encoders import SOFTWARE, global_args, graph_tail, video_codec_args
from core.ffmpeg import Capabilities, FFmpegTools, MediaInfo
from core.filtergraph import AUDIO_RATE, FilterGraph, fmt
from core.timeline import TimelineMap, build_timeline
from features.metadata import MetadataParams, metadata_args
from features.uniq import (
    UniqParams,
    output_fps,
    roll_uniq,
    silence_filters,
    source_audio_filters,
    source_video_filters,
)

MIN_KEPT_SECONDS = 1.0
VIDEO_PAD_SECONDS = 1.0  # clone-pad slack so every concat piece is exactly its length


@dataclass(frozen=True)
class JobContext:
    tools: FFmpegTools
    caps: Capabilities
    preset: Preset
    encoder: str  # key from core.encoders (libx264, nvenc, ...)
    work_dir: Path  # per-job temp dir; ffmpeg runs with this cwd


@dataclass(frozen=True)
class JobPlan:
    command: list[str]
    expected_duration: float
    uniq: UniqParams
    metadata: MetadataParams
    crf: int
    audio_bitrate_k: int
    timeline: TimelineMap
    notes: dict = field(default_factory=dict)  # music/subtitles/ads info for the report
    srt_text: str = ""


class InputList:
    """Collects ``-i`` inputs and hands out their stream indices."""

    def __init__(self) -> None:
        self.args: list[str] = []
        self._count = 0

    def add(self, *args: str) -> int:
        self.args += list(args)
        self._count += 1
        return self._count - 1


def kept_window(info: MediaInfo, params: UniqParams) -> tuple[float, float]:
    """Source interval left after random trims; trims are dropped if the clip is too short."""
    start, end = params.trim_start, info.duration - params.trim_end
    if end - start < MIN_KEPT_SECONDS:
        return 0.0, info.duration
    return start, end


def _exact_length(duration: float) -> list[str]:
    return [
        f"tpad=stop_mode=clone:stop_duration={fmt(VIDEO_PAD_SECONDS)}",
        f"trim=duration={fmt(duration, 4)}",
        "setpts=PTS-STARTPTS",
    ]


def _source_piece(
    graph: FilterGraph,
    inputs: InputList,
    info: MediaInfo,
    params: UniqParams,
    src_start: float,
    src_end: float,
    out_duration: float,
    target: tuple[int, int],
    fps: str,
) -> tuple[str, str]:
    idx = inputs.add("-ss", fmt(src_start, 4), "-t", fmt(src_end - src_start, 4), "-i", info.path)
    vfilters = source_video_filters(params, info.width, info.height, *target, fps)
    v = graph.one([f"{idx}:v:0"], vfilters + _exact_length(out_duration), "v")
    if info.has_audio:
        a = graph.one([f"{idx}:a:0"], source_audio_filters(params, out_duration), "a")
    else:
        a = graph.one([], silence_filters(out_duration), "a")
    return v, a


def plan_job(
    ctx: JobContext,
    info: MediaInfo,
    rng: random.Random,
    output_path: Path,
    metadata: MetadataParams,
    *,
    preview_seconds: float | None = None,
) -> JobPlan:
    preset = ctx.preset
    uniq = roll_uniq(preset.uniq, rng)
    crf = int(round(rng.uniform(preset.encoder.crf.min, preset.encoder.crf.max)))
    abr = int(round(rng.uniform(preset.encoder.audio_bitrate_k.min,
                                preset.encoder.audio_bitrate_k.max) / 8.0)) * 8

    target = (preset.uniq.target_width, preset.uniq.target_height)
    fps = output_fps(uniq, info.fps_float)
    start, end = kept_window(info, uniq)
    timeline = build_timeline(start, end, [], uniq.speed)

    graph = FilterGraph()
    inputs = InputList()
    pieces: list[str] = []
    for seg in timeline.segments:
        v, a = _source_piece(graph, inputs, info, uniq, seg.src_start, seg.src_end,
                             seg.out_duration, target, fps)
        pieces += [v, a]

    if len(pieces) > 2:
        v_out, a_out = graph.multi(pieces, f"concat=n={len(pieces) // 2}:v=1:a=1", ["v", "a"])
    else:
        v_out, a_out = pieces
    v_out = graph.one([v_out], ["format=yuv420p", *graph_tail(ctx.encoder)], "v")

    expected = timeline.duration
    if preview_seconds:
        expected = min(expected, preview_seconds)

    strip_sei = (ctx.encoder == SOFTWARE and preset.encoder.strip_encoder_sei
                 and "filter_units" in ctx.caps.bsfs)
    command = [
        ctx.tools.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
        "-progress", "pipe:1", "-nostats",
        *global_args(ctx.encoder),
        *inputs.args,
        "-filter_complex", graph.render(),
        "-map", f"[{v_out}]", "-map", f"[{a_out}]",
        *video_codec_args(ctx.encoder, crf, preset.encoder.preset, strip_sei),
        "-c:a", "aac", "-b:a", f"{abr}k", "-ar", str(AUDIO_RATE), "-ac", "2",
        *metadata_args(metadata),
        "-movflags", "+faststart",
        *(["-t", fmt(preview_seconds, 3)] if preview_seconds else []),
        "-f", "mp4", str(output_path),
    ]
    return JobPlan(
        command=command,
        expected_duration=expected,
        uniq=uniq,
        metadata=metadata,
        crf=crf,
        audio_bitrate_k=abr,
        timeline=timeline,
    )
