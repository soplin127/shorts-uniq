"""Per-file random uniqueness parameters and the filters that apply them."""
from __future__ import annotations

import random
from dataclasses import dataclass

from core.config import Range, UniqSettings
from core.filtergraph import AUDIO_FORMAT, AUDIO_RATE, even, fmt, fps_rational

EQ_BAND_FREQS = (120, 400, 1000, 3000, 8000)
EQ_BANDS_PER_FILE = 2
MAX_OUTPUT_FPS = 60.0


@dataclass(frozen=True)
class UniqParams:
    scale: float = 1.0
    zoom: float = 1.0
    crop_x: float = 0.5  # 0..1 position of the crop window inside the slack
    crop_y: float = 0.5
    flip: bool = False
    brightness: float = 0.0
    contrast: float = 1.0
    saturation: float = 1.0
    gamma: float = 1.0
    hue: float = 0.0
    noise: int = 0
    vignette: float = 0.0
    volume_db: float = 0.0
    eq_bands: tuple[tuple[int, float], ...] = ()
    pitch: float = 1.0
    speed: float = 1.0
    trim_start: float = 0.0
    trim_end: float = 0.0
    fps: float = 0.0  # 0 = keep source frame rate


def _uniform(rng: random.Random, r: Range) -> float:
    return rng.uniform(r.min, r.max)


def _signed(rng: random.Random, r: Range) -> float:
    """Magnitude ranges (min >= 0) get a random sign; signed ranges are sampled as-is."""
    value = _uniform(rng, r)
    if r.min >= 0 and rng.random() < 0.5:
        return -value
    return value


def roll_uniq(s: UniqSettings, rng: random.Random) -> UniqParams:
    """Sample one set of parameters. Disabled groups keep neutral values."""
    params: dict = {}
    if s.resolution_enabled:
        params.update(
            scale=1.0 + _signed(rng, s.scale_pct) / 100.0,
            zoom=1.0 + _uniform(rng, s.zoom_pct) / 100.0,
            crop_x=rng.random(),
            crop_y=rng.random(),
        )
    if s.color_enabled:
        params.update(
            brightness=_uniform(rng, s.brightness),
            contrast=_uniform(rng, s.contrast),
            saturation=_uniform(rng, s.saturation),
            gamma=_uniform(rng, s.gamma),
            hue=_uniform(rng, s.hue_deg),
        )
        if s.noise_enabled:
            params["noise"] = int(round(_uniform(rng, s.noise_strength)))
        if s.vignette_enabled:
            params["vignette"] = _uniform(rng, s.vignette_angle)
    if s.audio_enabled:
        params["volume_db"] = _signed(rng, s.volume_db)
        if s.eq_enabled:
            freqs = rng.sample(EQ_BAND_FREQS, EQ_BANDS_PER_FILE)
            params["eq_bands"] = tuple(
                (f, round(_uniform(rng, s.eq_gain_db), 2)) for f in sorted(freqs)
            )
        if s.pitch_enabled:
            params["pitch"] = 1.0 + _uniform(rng, s.pitch_pct) / 100.0
    if s.time_enabled:
        params.update(
            speed=_uniform(rng, s.speed),
            trim_start=_uniform(rng, s.trim_start),
            trim_end=_uniform(rng, s.trim_end),
        )
        if s.fps_enabled:
            params["fps"] = rng.choice(s.fps_choices)
    if s.flip_enabled:
        params["flip"] = rng.random() < s.flip_probability
    return UniqParams(**params)


def output_fps(params: UniqParams, source_fps: float) -> str:
    fps = params.fps or min(source_fps, MAX_OUTPUT_FPS)
    return fps_rational(fps)


def geometry_filters(
    params: UniqParams, src_w: int, src_h: int, target_w: int, target_h: int
) -> list[str]:
    """Cover-scale to the target, apply random scale/zoom, then crop/pad to exact size."""
    k = max(target_w / src_w, target_h / src_h) * params.scale * params.zoom
    sw, sh = even(src_w * k), even(src_h * k)
    filters = [f"scale={sw}:{sh}:flags=bicubic"]
    cw, ch = min(sw, target_w), min(sh, target_h)
    if (cw, ch) != (sw, sh):
        x = int(round((sw - cw) * params.crop_x))
        y = int(round((sh - ch) * params.crop_y))
        filters.append(f"crop={cw}:{ch}:{x}:{y}")
    if (cw, ch) != (target_w, target_h):
        filters.append(
            f"pad={target_w}:{target_h}:{(target_w - cw) // 2}:{(target_h - ch) // 2}:color=black"
        )
    filters.append("setsar=1")
    return filters


def color_filters(params: UniqParams) -> list[str]:
    filters = []
    if (params.brightness, params.contrast, params.saturation, params.gamma) != (0.0, 1.0, 1.0, 1.0):
        filters.append(
            f"eq=brightness={fmt(params.brightness)}:contrast={fmt(params.contrast)}"
            f":saturation={fmt(params.saturation)}:gamma={fmt(params.gamma)}"
        )
    if params.hue:
        filters.append(f"hue=h={fmt(params.hue, 2)}")
    if params.noise:
        filters.append(f"noise=alls={params.noise}:allf=t")
    if params.vignette:
        filters.append(f"vignette=angle={fmt(params.vignette)}")
    return filters


def source_video_filters(
    params: UniqParams, src_w: int, src_h: int, target_w: int, target_h: int, fps: str
) -> list[str]:
    filters = geometry_filters(params, src_w, src_h, target_w, target_h)
    if params.flip:
        filters.append("hflip")
    filters += color_filters(params)
    filters += [
        f"setpts=(PTS-STARTPTS)/{fmt(params.speed, 6)}",
        f"fps={fps}",
        "format=yuv420p",
    ]
    return filters


def source_audio_filters(params: UniqParams, out_duration: float) -> list[str]:
    filters = [AUDIO_FORMAT]
    if params.volume_db:
        filters.append(f"volume={fmt(params.volume_db, 2)}dB")
    for freq, gain in params.eq_bands:
        filters.append(f"equalizer=f={freq}:t=q:w=1:g={fmt(gain, 2)}")
    if params.pitch != 1.0:
        filters += [f"asetrate={int(round(AUDIO_RATE * params.pitch))}", f"aresample={AUDIO_RATE}"]
    tempo = params.speed / params.pitch
    if abs(tempo - 1.0) > 1e-4:
        filters.append(f"atempo={fmt(tempo, 6)}")
    filters += pad_audio_to(out_duration)
    return filters


def pad_audio_to(duration: float) -> list[str]:
    """Force an audio chain to an exact length so concat segments stay in sync."""
    return ["apad", f"atrim=duration={fmt(duration, 4)}", "asetpts=PTS-STARTPTS"]


def silence_filters(duration: float) -> list[str]:
    return [f"anullsrc=r={AUDIO_RATE}:cl=stereo", f"atrim=duration={fmt(duration, 4)}",
            "asetpts=PTS-STARTPTS"]
