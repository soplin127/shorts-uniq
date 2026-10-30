"""Ads: spliced-in clips and timed overlays (image, chroma-keyed video, text banner).

Slot times ("7", "50%", "random:20%-60%") refer to the uniqualized video *without*
ads. Clip insertions are fed into the timeline; overlays are mapped through it, so
an overlay stays on the same moment of content after clips are spliced in.
"""
from __future__ import annotations

import os
import random
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from core.config import AdSettings, AdSlot
from core.ffmpeg import FFmpegError, FFmpegTools, MediaInfo, probe
from core.filtergraph import AUDIO_FORMAT, FilterGraph, color_to_ffmpeg, even, fmt, quote_path
from core.sources import VIDEO_EXTENSIONS
from core.timeline import TimelineMap, parse_time_spec
from features.uniq import pad_audio_to

IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
MIN_AD_SECONDS = 0.3
CHROMA_BLEND = 0.05
BANNER_PADDING = 24
FONT_CANDIDATES = {
    "win32": [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf"],
    "darwin": [Path("/System/Library/Fonts/Supplemental/Arial.ttf"), Path("/Library/Fonts/Arial.ttf"),
               Path("/System/Library/Fonts/Helvetica.ttc")],
    "linux": [Path("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf"),
              Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")],
}


class AdError(FFmpegError):
    pass


@dataclass(frozen=True)
class ClipAd:
    info: MediaInfo
    at: float  # seconds into the ad-free output

    @property
    def note(self) -> str:
        return f"clip:{Path(self.info.path).name}@{self.at:.1f}s"


@dataclass(frozen=True)
class OverlayAd:
    slot: AdSlot
    kind: str  # "image" | "video" | "text"
    path: str
    start: float  # output time
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def fade(self) -> float:
        return min(self.slot.fade, self.duration / 2.0)

    @property
    def note(self) -> str:
        label = Path(self.path).name if self.path else "text"
        return f"{self.kind}:{label}@{self.start:.1f}s"


# ---------------------------------------------------------------- picking


def _media_files(path: str, extensions: frozenset[str]) -> list[Path]:
    root = Path(path).expanduser()
    if root.is_dir():
        return sorted((p for p in root.iterdir()
                       if p.is_file() and p.suffix.lower() in extensions and not p.name.startswith(".")),
                      key=lambda p: p.name.lower())
    return [root] if root.is_file() and root.suffix.lower() in extensions else []


def pick_file(path: str, extensions: frozenset[str], rng: random.Random) -> Path:
    files = _media_files(path, extensions)
    if not files:
        raise AdError(f"Рекламный файл не найден: {path} (нужны {', '.join(sorted(extensions))})")
    return rng.choice(files)


@lru_cache(maxsize=256)
def media_info(tools: FFmpegTools, path: str) -> MediaInfo:
    info = probe(tools, path)
    if not info.has_video:
        raise AdError(f"В рекламном файле нет видео: {path}")
    return info


def roll_clips(tools: FFmpegTools, ads: AdSettings, rng: random.Random, base_duration: float) -> list[ClipAd]:
    clips = []
    for slot in ads.slots if ads.enabled else ():
        if slot.mode != "clip":
            continue
        info = media_info(tools, str(pick_file(slot.path, VIDEO_EXTENSIONS, rng)))
        if info.duration < MIN_AD_SECONDS:
            raise AdError(f"Рекламный ролик слишком короткий: {info.path}")
        clips.append(ClipAd(info, parse_time_spec(slot.at, base_duration, rng)))
    return clips


def roll_overlays(
    ads: AdSettings,
    rng: random.Random,
    timeline: TimelineMap,
    base_to_source,  # callable: ad-free output seconds -> source seconds
    base_duration: float,
) -> list[OverlayAd]:
    overlays = []
    for slot in ads.slots if ads.enabled else ():
        if slot.mode != "overlay":
            continue
        if slot.path:
            path = pick_file(slot.path, IMAGE_EXTENSIONS | VIDEO_EXTENSIONS, rng)
            kind = "image" if path.suffix.lower() in IMAGE_EXTENSIONS else "video"
        else:  # config guarantees banner text when there is no file
            path, kind = None, "text"
        start = timeline.to_output(base_to_source(parse_time_spec(slot.at, base_duration, rng)))
        if start is None:
            continue
        # never run into a spliced clip or past the end of the video
        end = min(start + slot.duration, timeline.containing_segment_end(start))
        if end - start >= MIN_AD_SECONDS:
            overlays.append(OverlayAd(slot, kind, str(path) if path else "", start, end))
    return overlays


# ------------------------------------------------------------ clip pieces


def clip_video_filters(target: tuple[int, int], fps: str) -> list[str]:
    w, h = target
    return [
        f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=bicubic",
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black",
        "setsar=1",
        f"fps={fps}",
        "format=yuv420p",
    ]


def clip_audio_filters(duration: float) -> list[str]:
    return [AUDIO_FORMAT, *pad_audio_to(duration)]


# --------------------------------------------------------------- overlays


def position_xy(position: str, margin: int, obj_w: str, obj_h: str) -> tuple[str, str]:
    """Expressions for overlay/drawtext; W/H are the frame, obj_w/obj_h the object."""
    if position.endswith("left"):
        x = str(margin)
    elif position.endswith("right"):
        x = f"W-{obj_w}-{margin}"
    else:
        x = f"(W-{obj_w})/2"
    if position.startswith("top"):
        y = str(margin)
    elif position.startswith("bottom"):
        y = f"H-{obj_h}-{margin}"
    else:
        y = f"(H-{obj_h})/2"
    return x, y


def _enable(ov: OverlayAd) -> str:
    return f"enable='between(t,{fmt(ov.start, 3)},{fmt(ov.end, 3)})'"


def overlay_input_args(ov: OverlayAd, fps: str) -> list[str]:
    length = fmt(ov.duration, 3)
    if ov.kind == "image":
        return ["-loop", "1", "-framerate", fps, "-t", length, "-i", ov.path]
    return ["-stream_loop", "-1", "-t", length, "-i", ov.path]


def overlay_source_filters(ov: OverlayAd, frame_w: int) -> list[str]:
    filters = []
    if ov.kind == "video" and ov.slot.chroma_key:
        filters.append(f"chromakey={color_to_ffmpeg(ov.slot.chroma_key)}"
                       f":{fmt(ov.slot.chroma_similarity, 3)}:{CHROMA_BLEND}")
    filters += [f"scale={even(frame_w * ov.slot.scale)}:-2:flags=bicubic", "format=yuva420p"]
    if ov.fade > 0:
        filters += [
            f"fade=t=in:st=0:d={fmt(ov.fade, 3)}:alpha=1",
            f"fade=t=out:st={fmt(ov.duration - ov.fade, 3)}:d={fmt(ov.fade, 3)}:alpha=1",
        ]
    filters.append(f"setpts=PTS-STARTPTS+{fmt(ov.start, 3)}/TB")
    return filters


def default_font_file() -> Path | None:
    key = "linux" if sys.platform.startswith("linux") else sys.platform
    return next((p for p in FONT_CANDIDATES.get(key, []) if p.is_file()), None)


def banner_filter(ov: OverlayAd, text_file: str, font_file: Path | None) -> str:
    slot = ov.slot
    x, y = position_xy(slot.position, slot.margin, "tw", "th")
    a, b, f = fmt(ov.start, 3), fmt(ov.end, 3), fmt(max(ov.fade, 0.001), 3)
    alpha = f"if(lt(t,{a}+{f}),(t-{a})/{f},if(gt(t,{b}-{f}),({b}-t)/{f},1))"
    font = f"fontfile={quote_path(str(font_file))}" if font_file else "font=Sans"
    return (
        f"drawtext={font}:textfile={text_file}:expansion=none"
        f":fontsize={slot.text_size}:fontcolor={color_to_ffmpeg(slot.text_color)}"
        f":box=1:boxcolor={color_to_ffmpeg(slot.box_color)}:boxborderw={BANNER_PADDING}"
        f":x={x}:y={y}:alpha='{alpha}':{_enable(ov)}"
    )


def apply_overlays(
    graph: FilterGraph,
    add_input,  # callable(*args) -> input index
    video: str,
    overlays: list[OverlayAd],
    frame_w: int,
    fps: str,
    work_dir: Path,
) -> str:
    """Chain every overlay onto ``video`` and return the resulting label."""
    font_file = default_font_file()
    for i, ov in enumerate(overlays):
        if ov.kind == "text":
            text_file = f"banner_{i}.txt"  # relative: ffmpeg runs with cwd=work_dir
            (work_dir / text_file).write_text(ov.slot.text, encoding="utf-8")
            video = graph.one([video], [banner_filter(ov, text_file, font_file)], "v")
            continue
        idx = add_input(*overlay_input_args(ov, fps))
        layer = graph.one([f"{idx}:v:0"], overlay_source_filters(ov, frame_w), "v")
        x, y = position_xy(ov.slot.position, ov.slot.margin, "w", "h")
        video = graph.one([video, layer],
                          [f"overlay=x={x}:y={y}:eof_action=pass:{_enable(ov)}"], "v")
    return video
