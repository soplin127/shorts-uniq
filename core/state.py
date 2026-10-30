"""Resume support: a small manifest of finished jobs stored in the output folder."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

STATE_FILE = ".uniq_state.json"


class StateStore:
    """Thread-safe ``{job_key: output_name}`` map persisted atomically after each update."""

    def __init__(self, out_dir: Path):
        self._path = out_dir / STATE_FILE
        self._lock = threading.Lock()
        self._done: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        done = data.get("done") if isinstance(data, dict) else None
        return {str(k): str(v) for k, v in done.items()} if isinstance(done, dict) else {}

    @staticmethod
    def key(source: Path, variant: int) -> str:
        return f"{source.resolve()}|{variant}"

    def finished_output(self, source: Path, variant: int) -> Path | None:
        """Output of a finished job if it still exists on disk."""
        with self._lock:
            name = self._done.get(self.key(source, variant))
        if not name:
            return None
        path = self._path.parent / name
        return path if path.is_file() else None

    def mark_done(self, source: Path, variant: int, output: Path) -> None:
        with self._lock:
            self._done = {**self._done, self.key(source, variant): output.name}
            payload = json.dumps({"done": self._done}, ensure_ascii=False, indent=1)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self._path)
