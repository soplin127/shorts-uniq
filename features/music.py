"""Background music: track picking, looping, fades and sidechain ducking."""
from __future__ import annotations

import random
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from core.config import MusicSettings
from core.ffmpeg import FFmpegError, FFmpegTools, probe
from core.filtergraph import AUDIO_FORMAT, FilterGraph, fmt

AUDIO_EXTENSIONS = frozenset({".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"})
DUCK_ATTACK_MS = 20
DUCK_RELEASE_MS = 400
LIMITER_LEVEL = 0.95
MIN_TRACK_SECONDS = 0.5


class MusicError(FFmpegError):
    pass


@dataclass(frozen=True)
class MusicParams:
    track: str
    start: float  # offset into the track, seconds
    duration: float  # output length the music must cover
    fade_in: float
    fade_out: float

    @property
    def note(self) -> str:
        return f"{Path(self.track).name}@{self.start:.1f}s"


def is_audio(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS and not path.name.startswith(".")


def list_tracks(path: str) -> list[Path]:
    root = Path(path).expanduser()
    if root.is_dir():
        return sorted((p for p in root.iterdir() if is_audio(p)), key=lambda p: p.name.lower())
    return [root] if is_audio(root) else []


def pick_track(path: str, rng: random.Random) -> Path:
    tracks = list_tracks(path)
    if not tracks:
        raise MusicError(f"Музыка не найдена: {path} (нужны файлы {', '.join(sorted(AUDIO_EXTENSIONS))})")
    return rng.choice(tracks)


@lru_cache(maxsize=512)
def track_duration(tools: FFmpegTools, track: str) -> float:
    info = probe(tools, track)
    if not info.has_audio or info.duration < MIN_TRACK_SECONDS:
        raise MusicError(f"В файле нет пригодной аудиодорожки: {track}")
    return info.duration


def roll_music(
    settings: MusicSettings,
    rng: random.Random,
    track: Path,
    track_seconds: float,
    out_duration: float,
) -> MusicParams:
    """Pick a start offset; short tracks start at 0 and loop."""
    slack = track_seconds - out_duration
    start = rng.uniform(0.0, slack) if settings.random_start and slack > 0 else 0.0
    # Fades never overlap: each takes at most half of the clip.
    half = out_duration / 2.0
    return MusicParams(
        track=str(track),
        start=start,
        duration=out_duration,
        fade_in=min(settings.fade_in, half),
        fade_out=min(settings.fade_out, half),
    )


def music_input_args(params: MusicParams) -> list[str]:
    return ["-stream_loop", "-1", "-ss", fmt(params.start, 3), "-i", params.track]


def music_filters(settings: MusicSettings, params: MusicParams) -> list[str]:
    filters = [AUDIO_FORMAT, f"atrim=duration={fmt(params.duration, 4)}", "asetpts=PTS-STARTPTS"]
    if settings.volume_db:
        filters.append(f"volume={fmt(settings.volume_db, 2)}dB")
    if params.fade_in > 0:
        filters.append(f"afade=t=in:st=0:d={fmt(params.fade_in, 3)}")
    if params.fade_out > 0:
        start = params.duration - params.fade_out
        filters.append(f"afade=t=out:st={fmt(start, 3)}:d={fmt(params.fade_out, 3)}")
    return filters


def mix_music(
    graph: FilterGraph,
    original: str,
    music_input: int,
    settings: MusicSettings,
    params: MusicParams,
) -> str:
    """Return the label of the final audio: music alone, or music mixed under the original."""
    music = graph.one([f"{music_input}:a:0"], music_filters(settings, params), "a")
    if settings.replace_audio:
        graph.chain([original], ["anullsink"], "a", outputs=0)
        return music

    if settings.ducking:
        voice, sidechain = graph.chain([original], ["asplit=2"], "a", outputs=2)
        music = graph.one([music, sidechain], [
            f"sidechaincompress=threshold={fmt(settings.duck_threshold, 4)}"
            f":ratio={fmt(settings.duck_ratio, 2)}"
            f":attack={DUCK_ATTACK_MS}:release={DUCK_RELEASE_MS}"
        ], "a")
    else:
        voice = original

    if settings.original_volume_db:
        voice = graph.one([voice], [f"volume={fmt(settings.original_volume_db, 2)}dB"], "a")
    return graph.one([voice, music], [
        "amix=inputs=2:duration=first:normalize=0",
        f"alimiter=limit={LIMITER_LEVEL}",
    ], "a")
