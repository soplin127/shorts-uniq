"""Collecting input videos from folders, file lists and drag-and-drop."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".webm", ".mkv"})


def is_video(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS and not path.name.startswith(".")


def collect_sources(paths: Iterable[str | Path], recursive: bool = False) -> list[Path]:
    """Expand files/folders into a sorted, de-duplicated list of video files."""
    found: dict[str, Path] = {}
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            candidates = path.rglob("*") if recursive else path.iterdir()
            for item in candidates:
                if is_video(item):
                    found.setdefault(str(item.resolve()).lower(), item.resolve())
        elif is_video(path):
            found.setdefault(str(path.resolve()).lower(), path.resolve())
    return sorted(found.values(), key=lambda p: str(p).lower())
