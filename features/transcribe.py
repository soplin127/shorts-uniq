"""Speech-to-text with faster-whisper (optional dependency), cached on disk per source."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from pathlib import Path

from core.ffmpeg import FeatureUnavailable
from features.subtitles import Cue, Word

APP_NAME = "shorts-uniq"
CACHE_VERSION = 1
_lock = threading.Lock()  # one transcription at a time; variants then hit the cache
_models: dict[str, object] = {}


def cache_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / APP_NAME / "transcripts"


def cache_key(source: Path, model: str, language: str) -> str:
    stat = source.stat()
    raw = f"{CACHE_VERSION}|{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{model}|{language}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def cues_to_json(cues: list[Cue]) -> list[dict]:
    return [{"start": c.start, "end": c.end,
             "words": [[w.start, w.end, w.text] for w in c.words]} for c in cues]


def cues_from_json(data: list[dict]) -> list[Cue]:
    return [Cue(d["start"], d["end"], tuple(Word(*w) for w in d["words"])) for d in data]


def _load_model(name: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise FeatureUnavailable(
            "Автосубтитры требуют faster-whisper: pip install faster-whisper"
        ) from exc
    if name not in _models:
        _models[name] = WhisperModel(name, device="auto", compute_type="int8")
    return _models[name]


def _run_whisper(source: Path, model_name: str, language: str) -> list[Cue]:
    model = _load_model(model_name)
    segments, _info = model.transcribe(
        str(source), language=language or None, word_timestamps=True, vad_filter=True,
    )
    cues = []
    for seg in segments:
        words = tuple(Word(float(w.start), float(w.end), w.word.strip())
                      for w in (seg.words or []) if w.word.strip())
        if words:
            cues.append(Cue(float(seg.start), float(seg.end), words))
    return cues


def transcribe(source: Path, model_name: str, language: str = "") -> list[Cue]:
    """Word-timed cues in source time; reuses a cached result when the file is unchanged."""
    cache_file = cache_dir() / f"{cache_key(source, model_name, language)}.json"
    with _lock:
        if cache_file.is_file():
            try:
                return cues_from_json(json.loads(cache_file.read_text(encoding="utf-8")))
            except (ValueError, KeyError, TypeError):
                cache_file.unlink(missing_ok=True)  # corrupt cache entry: transcribe again
        cues = _run_whisper(source, model_name, language)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(cues_to_json(cues), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, cache_file)
        return cues
