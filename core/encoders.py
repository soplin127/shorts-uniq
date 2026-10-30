"""Video encoder selection: libx264 or a hardware encoder with automatic fallback."""
from __future__ import annotations

import os
import subprocess
import sys
from functools import lru_cache

from core.ffmpeg import popen_kwargs

SOFTWARE = "libx264"
HW_CODECS = {
    "nvenc": "h264_nvenc",
    "qsv": "h264_qsv",
    "amf": "h264_amf",
    "vaapi": "h264_vaapi",
    "videotoolbox": "h264_videotoolbox",
}
AUTO_ORDER = {
    "win32": ("nvenc", "qsv", "amf"),
    "darwin": ("videotoolbox",),
    "linux": ("nvenc", "vaapi", "qsv"),
}
DEFAULT_VAAPI_DEVICE = "/dev/dri/renderD128"
TEST_TIMEOUT_S = 20


def vaapi_device() -> str:
    return os.environ.get("VAAPI_DEVICE", DEFAULT_VAAPI_DEVICE)


def global_args(key: str) -> list[str]:
    """Options that must precede the inputs."""
    if key == "vaapi":
        return ["-vaapi_device", vaapi_device()]
    return []


def graph_tail(key: str) -> list[str]:
    """Filters appended to the final video chain (upload to GPU for VAAPI)."""
    if key == "vaapi":
        return ["format=nv12", "hwupload"]
    return []


def video_codec_args(key: str, crf: int, preset: str, strip_sei: bool) -> list[str]:
    """Map the CRF-style quality value onto each encoder's own rate control."""
    if key == "nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", str(crf),
                "-b:v", "0", "-profile:v", "high", "-pix_fmt", "yuv420p"]
    if key == "qsv":
        return ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", str(crf),
                "-pix_fmt", "nv12"]
    if key == "amf":
        return ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp",
                "-qp_i", str(crf), "-qp_p", str(crf + 2), "-pix_fmt", "yuv420p"]
    if key == "vaapi":
        return ["-c:v", "h264_vaapi", "-qp", str(crf)]
    if key == "videotoolbox":
        quality = max(1, min(100, 101 - 2 * crf))  # crf 18 -> 65, crf 23 -> 55
        return ["-c:v", "h264_videotoolbox", "-q:v", str(quality), "-profile:v", "high",
                "-pix_fmt", "yuv420p"]
    args = ["-c:v", SOFTWARE, "-preset", preset, "-crf", str(crf),
            "-profile:v", "high", "-pix_fmt", "yuv420p"]
    if strip_sei:
        # x264 embeds its version + full option string in an SEI NAL unit.
        args += ["-bsf:v", "filter_units=remove_types=6"]
    return args


@lru_cache(maxsize=16)
def encoder_works(ffmpeg: str, key: str) -> bool:
    """Run a tiny test encode; drivers/GPUs may be missing even if ffmpeg lists the codec."""
    if key == SOFTWARE:
        return True
    tail = ",".join(["format=yuv420p", *graph_tail(key)])
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", *global_args(key),
        "-f", "lavfi", "-i", "color=c=black:s=256x256:r=30:d=0.5",
        "-vf", tail, *video_codec_args(key, 23, "veryfast", False), "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=TEST_TIMEOUT_S,
                                stdin=subprocess.DEVNULL, **popen_kwargs())
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def resolve_encoder(ffmpeg: str, hwaccel: str, available: frozenset[str]) -> tuple[str, str]:
    """Return (encoder_key, warning). Falls back to libx264 when hardware is unusable."""
    if hwaccel == "none":
        return SOFTWARE, ""
    if hwaccel == "auto":
        for key in AUTO_ORDER.get(sys.platform, AUTO_ORDER["linux"]):
            if HW_CODECS[key] in available and encoder_works(ffmpeg, key):
                return key, ""
        return SOFTWARE, "Аппаратный энкодер не найден, используется libx264"
    codec = HW_CODECS.get(hwaccel)
    if codec and codec in available and encoder_works(ffmpeg, hwaccel):
        return hwaccel, ""
    return SOFTWARE, f"Энкодер {hwaccel} недоступен, используется libx264"
