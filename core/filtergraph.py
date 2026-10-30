"""Small helper for assembling ``-filter_complex`` strings with unique pad labels."""
from __future__ import annotations

from fractions import Fraction

AUDIO_RATE = 48000
AUDIO_FORMAT = f"aformat=sample_fmts=fltp:sample_rates={AUDIO_RATE}:channel_layouts=stereo"

# Rates that are conventionally expressed as NTSC fractions.
_NTSC_RATES = {23.976: "24000/1001", 29.97: "30000/1001", 59.94: "60000/1001"}


def even(value: float) -> int:
    """Round to the nearest even integer (>= 2); H.264 4:2:0 needs even dimensions."""
    return max(2, int(round(value / 2.0)) * 2)


def fps_rational(fps: float) -> str:
    for approx, rational in _NTSC_RATES.items():
        if abs(fps - approx) < 0.005:
            return rational
    frac = Fraction(fps).limit_denominator(1001)
    return str(frac.numerator) if frac.denominator == 1 else f"{frac.numerator}/{frac.denominator}"


def fmt(value: float, digits: int = 4) -> str:
    """Compact, locale-independent float formatting for filter arguments."""
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def quote_path(path: str) -> str:
    """Quote a filesystem path for use as a filter option value (handles C:\\ on Windows)."""
    escaped = path.replace("\\", "/").replace(":", "\\:").replace("'", "'\\\\''")
    return f"'{escaped}'"


def color_to_ffmpeg(color: str) -> str:
    """'#RRGGBB' or '#RRGGBB@0.5' -> '0xRRGGBB' / '0xRRGGBB@0.5'."""
    color = color.strip()
    if color.startswith("#"):
        return "0x" + color[1:]
    return color


class FilterGraph:
    """Accumulates filter chains; each chain gets fresh output labels."""

    def __init__(self) -> None:
        self._chains: list[str] = []
        self._counter = 0

    def _new_label(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    def chain(
        self,
        inputs: list[str],
        filters: list[str],
        kind: str = "v",
        outputs: int = 1,
    ) -> list[str]:
        """Add ``[in1][in2]f1,f2[out...]`` and return the output labels."""
        body = ",".join(filters) if filters else ("null" if kind == "v" else "anull")
        labels = [self._new_label(kind) for _ in range(outputs)]
        pads_in = "".join(f"[{name}]" for name in inputs)
        pads_out = "".join(f"[{name}]" for name in labels)
        self._chains.append(f"{pads_in}{body}{pads_out}")
        return labels

    def one(self, inputs: list[str], filters: list[str], kind: str = "v") -> str:
        return self.chain(inputs, filters, kind, 1)[0]

    def multi(self, inputs: list[str], body: str, kinds: list[str]) -> list[str]:
        """Add a filter with mixed-type outputs, e.g. ``concat`` -> ["v", "a"]."""
        labels = [self._new_label(kind) for kind in kinds]
        pads_in = "".join(f"[{name}]" for name in inputs)
        pads_out = "".join(f"[{name}]" for name in labels)
        self._chains.append(f"{pads_in}{body}{pads_out}")
        return labels

    def render(self) -> str:
        return ";".join(self._chains)
