"""Immutable settings model and JSON preset I/O.

Every settings block is a frozen dataclass; changes are made with
``dataclasses.replace`` so a preset can be shared safely between threads.
"""
from __future__ import annotations

import json
import types
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints


class ConfigError(ValueError):
    """Raised when a preset contains invalid values."""


@dataclass(frozen=True)
class Range:
    min: float
    max: float

    def __post_init__(self) -> None:
        if self.min > self.max:
            raise ConfigError(f"Range min {self.min} is greater than max {self.max}")


def _check_choice(name: str, value: str, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise ConfigError(f"{name} must be one of {allowed}, got {value!r}")


@dataclass(frozen=True)
class UniqSettings:
    resolution_enabled: bool = True
    target_width: int = 1080
    target_height: int = 1920
    scale_pct: Range = Range(2.0, 6.0)
    zoom_pct: Range = Range(1.0, 4.0)

    color_enabled: bool = True
    brightness: Range = Range(-0.03, 0.03)
    contrast: Range = Range(0.95, 1.07)
    saturation: Range = Range(0.92, 1.10)
    gamma: Range = Range(0.95, 1.05)
    hue_deg: Range = Range(-3.0, 3.0)
    noise_enabled: bool = False
    noise_strength: Range = Range(2.0, 6.0)
    vignette_enabled: bool = False
    vignette_angle: Range = Range(0.15, 0.40)

    audio_enabled: bool = True
    volume_db: Range = Range(1.0, 3.0)
    eq_enabled: bool = True
    eq_gain_db: Range = Range(-2.0, 2.0)
    pitch_enabled: bool = True
    pitch_pct: Range = Range(-1.0, 1.0)

    time_enabled: bool = True
    speed: Range = Range(0.97, 1.03)
    trim_start: Range = Range(0.0, 0.3)
    trim_end: Range = Range(0.0, 0.3)
    fps_enabled: bool = True
    fps_choices: tuple[float, ...] = (30.0, 29.97, 60.0)

    flip_enabled: bool = False
    flip_probability: float = 1.0

    def __post_init__(self) -> None:
        if self.target_width < 64 or self.target_height < 64:
            raise ConfigError("Target resolution is too small")
        if self.target_width % 2 or self.target_height % 2:
            raise ConfigError("Target resolution must use even numbers")
        if self.speed.min <= 0.5 or self.speed.max >= 2.0:
            raise ConfigError("Speed must stay within (0.5, 2.0)")
        if self.trim_start.min < 0 or self.trim_end.min < 0:
            raise ConfigError("Trim values must be non-negative")
        if not self.fps_choices or any(f <= 0 or f > 120 for f in self.fps_choices):
            raise ConfigError("fps_choices must contain values in (0, 120]")
        if not 0.0 <= self.flip_probability <= 1.0:
            raise ConfigError("flip_probability must be within [0, 1]")


HW_ACCEL_CHOICES = ("none", "auto", "nvenc", "qsv", "amf", "vaapi", "videotoolbox")
X264_PRESETS = (
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
)


@dataclass(frozen=True)
class EncoderSettings:
    crf: Range = Range(18.0, 23.0)
    preset: str = "veryfast"
    audio_bitrate_k: Range = Range(128.0, 192.0)
    hwaccel: str = "auto"
    strip_encoder_sei: bool = True

    def __post_init__(self) -> None:
        _check_choice("encoder.preset", self.preset, X264_PRESETS)
        _check_choice("encoder.hwaccel", self.hwaccel, HW_ACCEL_CHOICES)
        if self.crf.min < 0 or self.crf.max > 51:
            raise ConfigError("CRF must be within [0, 51]")
        if self.audio_bitrate_k.min < 32 or self.audio_bitrate_k.max > 512:
            raise ConfigError("Audio bitrate must be within [32, 512] kbit/s")


DEFAULT_NEUTRAL_TITLES = ("video", "clip", "IMG", "VID", "New video")


@dataclass(frozen=True)
class MetadataSettings:
    date_from: str = ""  # ISO date (YYYY-MM-DD); empty = 365 days ago
    date_to: str = ""  # ISO date; empty = today
    title_mode: str = "empty"
    neutral_titles: tuple[str, ...] = DEFAULT_NEUTRAL_TITLES
    verify: bool = True

    def __post_init__(self) -> None:
        _check_choice("metadata.title_mode", self.title_mode, ("empty", "neutral"))


@dataclass(frozen=True)
class MusicSettings:
    enabled: bool = False
    path: str = ""  # a track or a folder of tracks
    volume_db: float = -14.0
    original_volume_db: float = 0.0
    replace_audio: bool = False
    ducking: bool = True
    duck_ratio: float = 6.0
    duck_threshold: float = 0.05
    random_start: bool = True
    fade_in: float = 1.0
    fade_out: float = 1.5

    def __post_init__(self) -> None:
        if not 1.0 <= self.duck_ratio <= 20.0:
            raise ConfigError("music.duck_ratio must be within [1, 20]")
        if not 0.0 < self.duck_threshold <= 1.0:
            raise ConfigError("music.duck_threshold must be within (0, 1]")
        if self.fade_in < 0 or self.fade_out < 0:
            raise ConfigError("music fades must be non-negative")


SUB_POSITIONS = ("bottom", "center", "top")


@dataclass(frozen=True)
class SubtitleSettings:
    enabled: bool = False
    source: str = "file"  # "file" (same-name .srt/.ass) or "auto" (faster-whisper)
    whisper_model: str = "small"
    language: str = ""  # empty = autodetect
    font: str = "Arial"
    font_size: int = 72
    bold: bool = True
    uppercase: bool = False
    primary_color: str = "#FFFFFF"
    highlight_color: str = "#FFE000"
    outline_color: str = "#000000"
    outline: float = 4.0
    shadow: float = 2.0
    position: str = "bottom"
    margin_v: int = 320
    shorts_style: bool = True
    max_words: int = 3
    highlight_current_word: bool = False
    save_srt: bool = False
    fonts_dir: str = ""

    def __post_init__(self) -> None:
        _check_choice("subtitles.source", self.source, ("file", "auto"))
        _check_choice("subtitles.position", self.position, SUB_POSITIONS)
        if not 1 <= self.max_words <= 10:
            raise ConfigError("subtitles.max_words must be within [1, 10]")
        if self.font_size < 8:
            raise ConfigError("subtitles.font_size is too small")


AD_POSITIONS = (
    "top-left", "top", "top-right",
    "center",
    "bottom-left", "bottom", "bottom-right",
)


@dataclass(frozen=True)
class AdSlot:
    mode: str = "clip"  # "clip" (spliced in) or "overlay"
    path: str = ""  # file or folder (random pick); empty + text = text banner
    at: str = "50%"  # "7", "00:07", "35%", "random:3-10", "random:20%-60%"
    duration: float = 5.0  # overlay only, seconds of output time
    position: str = "bottom"
    scale: float = 0.6  # overlay width as fraction of frame width
    margin: int = 80
    fade: float = 0.3
    chroma_key: str = ""  # e.g. "#00FF00" for green-screen video overlays
    chroma_similarity: float = 0.15
    text: str = ""
    text_size: int = 56
    text_color: str = "#FFFFFF"
    box_color: str = "#000000@0.6"

    def __post_init__(self) -> None:
        _check_choice("ad.mode", self.mode, ("clip", "overlay"))
        _check_choice("ad.position", self.position, AD_POSITIONS)
        if not 0.05 <= self.scale <= 1.0:
            raise ConfigError("ad.scale must be within [0.05, 1]")
        if self.duration <= 0:
            raise ConfigError("ad.duration must be positive")
        if self.fade < 0:
            raise ConfigError("ad.fade must be non-negative")
        if not self.path and not (self.mode == "overlay" and self.text):
            raise ConfigError("ad slot needs a path (or text for an overlay banner)")


@dataclass(frozen=True)
class AdSettings:
    enabled: bool = False
    slots: tuple[AdSlot, ...] = ()


@dataclass(frozen=True)
class OutputSettings:
    input_dir: str = ""
    recursive: bool = False
    output_dir: str = ""
    name_template: str = "IMG_{random4}_{date}"
    variants: int = 1

    def __post_init__(self) -> None:
        if not 1 <= self.variants <= 100:
            raise ConfigError("output.variants must be within [1, 100]")
        if not self.name_template.strip():
            raise ConfigError("output.name_template must not be empty")


@dataclass(frozen=True)
class BatchSettings:
    workers: int = 0  # 0 = cpu_count // 2
    seed: str = ""  # empty = non-reproducible
    skip_existing: bool = True
    preview_seconds: float = 10.0
    ffmpeg_path: str = ""  # empty = FFMPEG_BINARY env var or PATH

    def __post_init__(self) -> None:
        if self.workers < 0:
            raise ConfigError("batch.workers must be >= 0")
        if self.preview_seconds <= 0:
            raise ConfigError("batch.preview_seconds must be positive")


@dataclass(frozen=True)
class Preset:
    uniq: UniqSettings = field(default_factory=UniqSettings)
    encoder: EncoderSettings = field(default_factory=EncoderSettings)
    metadata: MetadataSettings = field(default_factory=MetadataSettings)
    music: MusicSettings = field(default_factory=MusicSettings)
    subtitles: SubtitleSettings = field(default_factory=SubtitleSettings)
    ads: AdSettings = field(default_factory=AdSettings)
    output: OutputSettings = field(default_factory=OutputSettings)
    batch: BatchSettings = field(default_factory=BatchSettings)


# --------------------------------------------------------------------------- I/O


def _coerce(tp: Any, value: Any, where: str) -> Any:
    origin = get_origin(tp)
    if tp is Range:
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return Range(float(value[0]), float(value[1]))
        if isinstance(value, dict) and {"min", "max"} <= set(value):
            return Range(float(value["min"]), float(value["max"]))
        raise ConfigError(f"{where}: expected [min, max]")
    if is_dataclass(tp):
        if not isinstance(value, dict):
            raise ConfigError(f"{where}: expected an object")
        return _build(tp, value, where)
    if origin is tuple:
        if not isinstance(value, (list, tuple)):
            raise ConfigError(f"{where}: expected a list")
        elem = get_args(tp)[0]
        return tuple(_coerce(elem, v, f"{where}[{i}]") for i, v in enumerate(value))
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(tp) if a is not type(None)]
        return None if value is None else _coerce(args[0], value, where)
    if tp is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: expected true/false")
        return value
    if tp in (int, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}: expected a number")
        if tp is int and float(value) != int(value):
            raise ConfigError(f"{where}: expected an integer")
        return tp(value)
    if tp is str:
        if not isinstance(value, str):
            raise ConfigError(f"{where}: expected a string")
        return value
    raise ConfigError(f"{where}: unsupported type {tp}")


def _build(cls: type, data: dict, where: str) -> Any:
    hints = get_type_hints(cls)
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
    kwargs = {
        name: _coerce(hints[name], value, f"{where}.{name}")
        for name, value in data.items()
    }
    try:
        return cls(**kwargs)
    except ConfigError as exc:
        raise ConfigError(f"{where}: {exc}") from exc


def preset_from_dict(data: dict) -> Preset:
    if not isinstance(data, dict):
        raise ConfigError("Preset must be a JSON object")
    return _build(Preset, data, "preset")


def preset_to_dict(preset: Preset) -> dict:
    return asdict(preset)


def load_preset(path: str | Path) -> Preset:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read preset {path}: {exc}") from exc
    return preset_from_dict(data)


def save_preset(preset: Preset, path: str | Path) -> None:
    text = json.dumps(preset_to_dict(preset), ensure_ascii=False, indent=2)
    Path(path).write_text(text + "\n", encoding="utf-8")
