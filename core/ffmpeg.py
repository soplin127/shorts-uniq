"""FFmpeg / ffprobe discovery, probing, capability detection and process control.

All commands are passed to ``subprocess`` as argument lists (never ``shell=True``)
so file names with spaces, quotes or non-ASCII characters are safe on every OS.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable

try:  # psutil gives cross-platform suspend/resume (Windows has no SIGSTOP)
    import psutil
except ImportError:  # pragma: no cover - psutil is in requirements.txt
    psutil = None


class FFmpegError(RuntimeError):
    """An ffmpeg/ffprobe invocation failed."""


class FFmpegNotFound(FFmpegError):
    """ffmpeg or ffprobe binary is missing."""


class FeatureUnavailable(FFmpegError):
    """The installed ffmpeg build lacks a filter/encoder a feature needs."""


IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
EXE_SUFFIX = ".exe" if IS_WINDOWS else ""
STDERR_TAIL_LINES = 25

# Homebrew's plain "ffmpeg" formula ships without libass/drawtext; the keg-only
# "ffmpeg-full" has them, so prefer it when present.
MACOS_FULL_BUILD_DIRS = (
    "/opt/homebrew/opt/ffmpeg-full/bin",
    "/usr/local/opt/ffmpeg-full/bin",
)


def install_hint() -> str:
    if IS_WINDOWS:
        return (
            "Установите FFmpeg: winget install Gyan.FFmpeg\n"
            "или скачайте full build с https://www.gyan.dev/ffmpeg/builds/ "
            "и добавьте папку bin в PATH (или укажите путь в настройках)."
        )
    if IS_MACOS:
        return (
            "Установите FFmpeg: brew install ffmpeg-full\n"
            "(обычная формула ffmpeg собрана без libass/drawtext — "
            "субтитры и текстовые баннеры работать не будут)."
        )
    if Path("/etc/fedora-release").exists():
        return (
            "Установите FFmpeg из RPM Fusion:\n"
            "  sudo dnf install https://mirrors.rpmfusion.org/free/fedora/"
            "rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm\n"
            "  sudo dnf install ffmpeg --allowerasing"
        )
    return "Установите FFmpeg через пакетный менеджер, например: sudo apt install ffmpeg"


def popen_kwargs() -> dict:
    """Extra Popen kwargs: hide console windows when launched from the GUI on Windows."""
    if IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


@dataclass(frozen=True)
class FFmpegTools:
    ffmpeg: str
    ffprobe: str


def _binary_in(directory: str | Path, name: str) -> str | None:
    candidate = Path(directory) / f"{name}{EXE_SUFFIX}"
    return str(candidate) if candidate.is_file() else None


def _candidate_dirs(custom: str) -> Iterable[str]:
    if custom:
        path = Path(custom).expanduser()
        yield str(path.parent if path.is_file() else path)
    env = os.environ.get("FFMPEG_BINARY", "")
    if env:
        yield str(Path(env).expanduser().parent)
    if IS_MACOS:
        yield from MACOS_FULL_BUILD_DIRS
    yield str(Path(__file__).resolve().parent.parent / "bin")


def locate_tools(custom: str = "") -> FFmpegTools:
    """Find ffmpeg + ffprobe: custom path, $FFMPEG_BINARY, full macOS build, ./bin, PATH."""
    for directory in _candidate_dirs(custom):
        ffmpeg = _binary_in(directory, "ffmpeg")
        ffprobe = _binary_in(directory, "ffprobe")
        if ffmpeg and ffprobe:
            return FFmpegTools(ffmpeg, ffprobe)
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        return FFmpegTools(ffmpeg, ffprobe)
    missing = "ffmpeg" if not ffmpeg else "ffprobe"
    raise FFmpegNotFound(f"Не найден {missing}.\n{install_hint()}")


def _run_capture(cmd: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            **popen_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FFmpegError(f"Не удалось запустить {cmd[0]}: {exc}") from exc


# ------------------------------------------------------------------ capabilities


@dataclass(frozen=True)
class Capabilities:
    version: str
    filters: frozenset[str]
    encoders: frozenset[str]
    bsfs: frozenset[str]

    def require_filter(self, name: str, feature: str) -> None:
        if name not in self.filters:
            raise FeatureUnavailable(
                f"{feature}: в установленной сборке FFmpeg нет фильтра '{name}'.\n"
                f"{install_hint()}"
            )


def _parse_listing(text: str, min_flag_len: int) -> frozenset[str]:
    names = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) >= min_flag_len and parts[0] != "------":
            if set(parts[0]) <= set("TSCVAD.|NFXBI"):
                names.add(parts[1])
    return frozenset(names)


@lru_cache(maxsize=8)
def capabilities(ffmpeg: str) -> Capabilities:
    version_out = _run_capture([ffmpeg, "-hide_banner", "-version"]).stdout
    version = version_out.splitlines()[0] if version_out else "unknown"
    filters = _parse_listing(_run_capture([ffmpeg, "-hide_banner", "-filters"]).stdout, 2)
    encoders = _parse_listing(_run_capture([ffmpeg, "-hide_banner", "-encoders"]).stdout, 6)
    bsf_out = _run_capture([ffmpeg, "-hide_banner", "-bsfs"]).stdout
    bsfs = frozenset(
        line.strip() for line in bsf_out.splitlines()
        if line.strip() and not line.strip().endswith(":")
    )
    return Capabilities(version, filters, encoders, bsfs)


# ----------------------------------------------------------------------- probing


@dataclass(frozen=True)
class MediaInfo:
    path: str
    duration: float
    has_video: bool
    width: int = 0  # display size, rotation applied
    height: int = 0
    fps: str = "30"  # rational string, e.g. "30000/1001"
    has_audio: bool = False
    audio_rate: int = 0
    tags: dict = field(default_factory=dict)  # flattened format + stream tags

    @property
    def fps_float(self) -> float:
        return float(Fraction(self.fps))


def _stream_rotation(stream: dict) -> int:
    for side in stream.get("side_data_list", []) or []:
        if "rotation" in side:
            return int(float(side["rotation"]))
    rotate = (stream.get("tags") or {}).get("rotate")
    return int(float(rotate)) if rotate else 0


def _valid_rate(rate: str | None) -> str | None:
    if not rate or rate in ("0/0", "0"):
        return None
    try:
        value = Fraction(rate)
    except (ValueError, ZeroDivisionError):
        return None
    return rate if 0 < value <= 240 else None


def parse_probe(path: str, data: dict) -> MediaInfo:
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = float(fmt.get("duration") or 0.0)
    if duration <= 0 and video:
        duration = float(video.get("duration") or 0.0)

    tags: dict[str, str] = {f"format.{k}": str(v) for k, v in (fmt.get("tags") or {}).items()}
    for stream in streams:
        prefix = f"stream{stream.get('index', 0)}"
        for key, value in (stream.get("tags") or {}).items():
            tags[f"{prefix}.{key}"] = str(value)

    width = height = 0
    fps = "30"
    if video:
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
        if abs(_stream_rotation(video)) % 180 == 90:
            width, height = height, width
        fps = (_valid_rate(video.get("avg_frame_rate"))
               or _valid_rate(video.get("r_frame_rate")) or "30")

    return MediaInfo(
        path=path,
        duration=duration,
        has_video=video is not None,
        width=width,
        height=height,
        fps=fps,
        has_audio=audio is not None,
        audio_rate=int(audio.get("sample_rate") or 0) if audio else 0,
        tags=tags,
    )


def probe(tools: FFmpegTools, path: str | Path) -> MediaInfo:
    cmd = [
        tools.ffprobe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    result = _run_capture(cmd)
    if result.returncode != 0:
        raise FFmpegError(f"ffprobe не смог прочитать {path}: {result.stderr.strip()[-500:]}")
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise FFmpegError(f"ffprobe вернул некорректный JSON для {path}") from exc
    return parse_probe(str(path), data)


def probe_raw(tools: FFmpegTools, path: str | Path) -> dict:
    result = _run_capture([
        tools.ffprobe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", "-show_chapters", str(path),
    ])
    if result.returncode != 0:
        raise FFmpegError(f"ffprobe: {result.stderr.strip()[-500:]}")
    return json.loads(result.stdout or "{}")


# ------------------------------------------------------------- process control


class FFmpegProcess:
    """A running ffmpeg with progress parsing and suspend/resume/terminate."""

    def __init__(self, cmd: list[str], cwd: str | None, stderr_path: Path):
        self._stderr_path = stderr_path
        self._stderr_file = open(stderr_path, "w", encoding="utf-8", errors="replace")
        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=self._stderr_file,
                text=True,
                encoding="utf-8",
                errors="replace",
                **popen_kwargs(),
            )
        except OSError as exc:
            self._stderr_file.close()
            raise FFmpegError(f"Не удалось запустить ffmpeg: {exc}") from exc
        self.suspended = False

    def progress_seconds(self) -> Iterable[float]:
        """Yield encoded output time (seconds) parsed from ``-progress pipe:1``."""
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            key, _, value = line.strip().partition("=")
            if key in ("out_time_us", "out_time_ms") and value.lstrip("-").isdigit():
                yield max(0.0, int(value) / 1_000_000)

    def wait(self) -> int:
        code = self.proc.wait()
        self._stderr_file.close()
        return code

    def stderr_tail(self) -> str:
        try:
            with open(self._stderr_path, encoding="utf-8", errors="replace") as fh:
                return "".join(deque(fh, maxlen=STDERR_TAIL_LINES)).strip()
        except OSError:
            return ""

    def suspend(self) -> None:
        if self.proc.poll() is not None or self.suspended:
            return
        if psutil is not None:
            psutil.Process(self.proc.pid).suspend()
        elif not IS_WINDOWS:
            os.kill(self.proc.pid, signal.SIGSTOP)
        else:
            return  # no way to suspend without psutil; queue-level pause still applies
        self.suspended = True

    def resume(self) -> None:
        if self.proc.poll() is not None or not self.suspended:
            return
        if psutil is not None:
            psutil.Process(self.proc.pid).resume()
        elif not IS_WINDOWS:
            os.kill(self.proc.pid, signal.SIGCONT)
        self.suspended = False

    def terminate(self) -> None:
        if self.proc.poll() is not None:
            return
        try:
            self.resume()
        except Exception:  # noqa: BLE001 - best effort before killing
            pass
        self.proc.kill()


def run_ffmpeg(
    cmd: list[str],
    *,
    cwd: str | None,
    stderr_path: Path,
    expected_duration: float,
    on_progress: Callable[[float], None] | None = None,
    on_start: Callable[[FFmpegProcess], None] | None = None,
) -> None:
    """Run ffmpeg to completion, reporting progress as a 0..1 fraction."""
    process = FFmpegProcess(cmd, cwd, stderr_path)
    if on_start:
        on_start(process)
    for seconds in process.progress_seconds():
        if on_progress and expected_duration > 0:
            on_progress(min(1.0, seconds / expected_duration))
    code = process.wait()
    if code != 0:
        raise FFmpegError(f"ffmpeg завершился с кодом {code}:\n{process.stderr_tail()}")
    if on_progress:
        on_progress(1.0)
