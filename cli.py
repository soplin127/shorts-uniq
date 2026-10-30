"""Command-line interface: python cli.py -i ./in -o ./out --variants 3"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from core.batch import BatchCallbacks, BatchRunner
from core.config import HW_ACCEL_CHOICES, ConfigError, Preset, load_preset
from core.ffmpeg import FFmpegNotFound, locate_tools
from core.sources import collect_sources


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Уникализация коротких видео для TikTok / Shorts")
    p.add_argument("-i", "--input", nargs="+", required=True, help="папки и/или файлы")
    p.add_argument("-o", "--output", required=True, help="папка результата")
    p.add_argument("-r", "--recursive", action="store_true", help="искать во вложенных папках")
    p.add_argument("--preset", help="JSON-пресет")
    p.add_argument("--variants", type=int, help="вариантов на каждый исходник")
    p.add_argument("--name-template", help="шаблон имени, напр. IMG_{random4}_{date}")
    p.add_argument("--workers", type=int, help="параллельных задач (0 = ядра/2)")
    p.add_argument("--hwaccel", choices=HW_ACCEL_CHOICES, help="аппаратный энкодер")
    p.add_argument("--seed", help="сид для воспроизводимости")
    p.add_argument("--no-skip", action="store_true", help="не пропускать уже готовые")
    p.add_argument("--preview", action="store_true", help="только первый файл, первые N секунд")
    p.add_argument("--ffmpeg", help="путь к ffmpeg (или папке с ним)")
    return p


def apply_args(preset: Preset, args: argparse.Namespace) -> Preset:
    output = preset.output
    if args.variants is not None:
        output = replace(output, variants=args.variants)
    if args.name_template:
        output = replace(output, name_template=args.name_template)
    batch = preset.batch
    if args.workers is not None:
        batch = replace(batch, workers=args.workers)
    if args.seed is not None:
        batch = replace(batch, seed=args.seed)
    if args.no_skip:
        batch = replace(batch, skip_existing=False)
    if args.ffmpeg:
        batch = replace(batch, ffmpeg_path=args.ffmpeg)
    encoder = replace(preset.encoder, hwaccel=args.hwaccel) if args.hwaccel else preset.encoder
    return replace(preset, output=output, batch=batch, encoder=encoder)


def _format_eta(eta: float | None) -> str:
    if eta is None:
        return "--:--"
    minutes, seconds = divmod(int(eta), 60)
    return f"{minutes:02d}:{seconds:02d}"


def console_callbacks() -> BatchCallbacks:
    def on_overall(done: int, total: int, eta: float | None) -> None:
        sys.stderr.write(f"\r[{done}/{total}] ETA {_format_eta(eta)}   ")
        sys.stderr.flush()

    def on_log(line: str) -> None:
        sys.stderr.write("\r" + " " * 40 + "\r")
        print(line, flush=True)

    return BatchCallbacks(on_overall=on_overall, on_log=on_log)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        preset = load_preset(args.preset) if args.preset else Preset()
        preset = apply_args(preset, args)
    except ConfigError as exc:
        print(f"Ошибка пресета: {exc}", file=sys.stderr)
        return 2
    try:
        tools = locate_tools(preset.batch.ffmpeg_path)
    except FFmpegNotFound as exc:
        print(exc, file=sys.stderr)
        return 3

    sources = collect_sources(args.input, args.recursive or preset.output.recursive)
    if not sources:
        print("Не найдено видео (mp4/mov/webm/mkv)", file=sys.stderr)
        return 1
    if args.preview:
        sources = sources[:1]

    runner = BatchRunner(tools, preset, sources, Path(args.output), console_callbacks(),
                         preview=args.preview)
    try:
        results = runner.run()
    except KeyboardInterrupt:
        runner.stop()
        print("\nПрервано", file=sys.stderr)
        return 130
    sys.stderr.write("\n")
    return 0 if all(r.status in ("done", "skipped") for r in results) else 4


if __name__ == "__main__":
    sys.exit(main())
