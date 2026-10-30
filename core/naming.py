"""Output file naming from templates like ``IMG_{random4}_{date}``."""
from __future__ import annotations

import random
import re
import uuid
from pathlib import Path

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
TOKEN = re.compile(r"\{(\w+)\}")
MAX_STEM_LEN = 120


def _sanitize(name: str) -> str:
    name = INVALID_CHARS.sub("_", name).strip().rstrip(".")
    if not name or name.upper() in WINDOWS_RESERVED:
        name = f"_{name}"
    return name[:MAX_STEM_LEN]


def render_name(
    template: str,
    rng: random.Random,
    *,
    date_compact: str,
    time_compact: str,
    stem: str,
    variant: int,
    index: int,
) -> str:
    """Render a file stem. Unknown tokens are left as-is so typos stay visible."""

    def token(match: re.Match) -> str:
        key = match.group(1)
        if key.startswith("random") and key[6:].isdigit():
            digits = max(1, min(12, int(key[6:])))
            return "".join(rng.choice("0123456789") for _ in range(digits))
        values = {
            "date": date_compact,
            "time": time_compact,
            "uuid": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
            "stem": stem,
            "variant": str(variant),
            "index": f"{index:04d}",
        }
        return values.get(key, match.group(0))

    return _sanitize(TOKEN.sub(token, template))


def pick_output_path(out_dir: Path, make_stem, taken: set[str], attempts: int = 50) -> Path:
    """Call ``make_stem()`` until the name is free on disk and not reserved by another job."""
    for _ in range(attempts):
        candidate = out_dir / f"{make_stem()}.mp4"
        key = candidate.name.lower()
        if key not in taken and not candidate.exists():
            taken.add(key)
            return candidate
    raise RuntimeError("Не удалось подобрать свободное имя файла — добавьте {random4} или {uuid} в шаблон")
