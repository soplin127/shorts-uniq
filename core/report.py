"""CSV report with one row per job (parameters included for reproducibility)."""
from __future__ import annotations

import csv
import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

COLUMNS = (
    "source", "output", "variant", "status", "error", "elapsed_s",
    "encoder", "crf", "audio_kbps", "music", "subtitles", "ads", "params_json",
)


@dataclass(frozen=True)
class ReportRow:
    source: str
    output: str = ""
    variant: int = 1
    status: str = ""
    error: str = ""
    elapsed_s: float = 0.0
    encoder: str = ""
    crf: int = 0
    audio_kbps: int = 0
    music: str = ""
    subtitles: str = ""
    ads: str = ""
    params_json: str = ""


def params_to_json(params) -> str:
    return json.dumps(asdict(params), ensure_ascii=False, sort_keys=True)


class CsvReport:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        # BOM (utf-8-sig) on creation so Excel on Windows opens Cyrillic file names correctly
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            csv.writer(fh).writerow(COLUMNS)

    def add(self, row: ReportRow) -> None:
        values = asdict(row)
        with self._lock, open(self.path, "a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow([values[c] for c in COLUMNS])
