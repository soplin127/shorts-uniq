"""Command-line interface: python cli.py -i ./in -o ./out --variants 3"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from core.batch import BatchCallbacks, BatchRunner, format_eta
from core.config import (
    AD_POSITIONS,
    HW_ACCEL_CHOICES,
    AdSettings,
    AdSlot,
    ConfigError,
    WHISPER_MODELS,
    Preset,
    load_preset,
)
from core.ffmpeg import FFmpegNotFound, locate_tools
from core.sources import collect_sources

SUBS_CHOICES = ("auto", "file", "none")


def _add_job_args(p: argparse.ArgumentParser) -> None:
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


def _add_music_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("музыка")
    g.add_argument("--music", metavar="PATH", help="трек или папка с треками (случайный выбор)")
    g.add_argument("--music-volume", type=float, metavar="DB", help="громкость музыки, дБ")
    g.add_argument("--replace-audio", action="store_true", help="заменить оригинальный звук")
    g.add_argument("--no-duck", action="store_true", help="не приглушать музыку под голосом")


def _add_subtitle_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("субтитры")
    g.add_argument("--subs", choices=SUBS_CHOICES,
                   help="auto = whisper, file = одноимённый .srt/.ass, none = выключить")
    g.add_argument("--whisper-model", choices=WHISPER_MODELS, help="модель faster-whisper")
    g.add_argument("--lang", help="язык речи (ru, en, ...); пусто = автоопределение")
    g.add_argument("--max-words", type=int, help="слов на экране (стиль Shorts)")
    g.add_argument("--highlight", action="store_true", help="подсвечивать текущее слово")
    g.add_argument("--save-srt", action="store_true", help="сохранить .srt рядом с результатом")


def _add_ad_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("реклама (флаги --ad/--ad-text заменяют слоты пресета)")
    g.add_argument("--ad", action="append", default=[], metavar="PATH",
                   help="ролик/картинка или папка; можно повторять")
    g.add_argument("--ad-text", action="append", default=[], metavar="TEXT",
                   help="текстовый баннер; можно повторять")
    g.add_argument("--ad-at", action="append", default=[], metavar="WHEN",
                   help="7 | 00:07 | 35%% | random:20%%-60%%; по одному на --ad/--ad-text "
                        "или один на все")
    g.add_argument("--ad-mode", action="append", default=[], choices=("clip", "overlay"),
                   help="clip = вставка, overlay = поверх видео (по умолчанию clip)")
    g.add_argument("--ad-duration", type=float, help="длительность оверлея, с")
    g.add_argument("--ad-position", choices=AD_POSITIONS, help="позиция оверлея")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Уникализация коротких видео для TikTok / Shorts")
    _add_job_args(p)
    _add_music_args(p)
    _add_subtitle_args(p)
    _add_ad_args(p)
    return p


# ------------------------------------------------------------ preset overrides


def _apply_output_batch(preset: Preset, args: argparse.Namespace) -> Preset:
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


def _apply_music(preset: Preset, args: argparse.Namespace) -> Preset:
    music = preset.music
    if args.music:
        music = replace(music, enabled=True, path=args.music)
    if args.music_volume is not None:
        music = replace(music, volume_db=args.music_volume)
    if args.replace_audio:
        music = replace(music, replace_audio=True)
    if args.no_duck:
        music = replace(music, ducking=False)
    return replace(preset, music=music)


def _apply_subtitles(preset: Preset, args: argparse.Namespace) -> Preset:
    subs = preset.subtitles
    if args.subs == "none":
        subs = replace(subs, enabled=False)
    elif args.subs:
        subs = replace(subs, enabled=True, source=args.subs)
    overrides = {
        "whisper_model": args.whisper_model,
        "language": args.lang,
        "max_words": args.max_words,
        "highlight_current_word": True if args.highlight else None,
        "save_srt": True if args.save_srt else None,
    }
    subs = replace(subs, **{k: v for k, v in overrides.items() if v is not None})
    return replace(preset, subtitles=subs)


def _nth(values: list[str], index: int, default: str) -> str:
    """The value for the index-th ad; a shorter list repeats its last value."""
    if not values:
        return default
    return values[min(index, len(values) - 1)]


def ad_slots_from_args(args: argparse.Namespace) -> tuple[AdSlot, ...]:
    extra = {}
    if args.ad_duration is not None:
        extra["duration"] = args.ad_duration
    if args.ad_position:
        extra["position"] = args.ad_position
    slots = [
        AdSlot(mode=_nth(args.ad_mode, i, "clip"), path=path,
               at=_nth(args.ad_at, i, "50%"), **extra)
        for i, path in enumerate(args.ad)
    ]
    offset = len(slots)
    slots += [
        AdSlot(mode="overlay", text=text, at=_nth(args.ad_at, offset + i, "50%"), **extra)
        for i, text in enumerate(args.ad_text)
    ]
    return tuple(slots)


def _apply_ads(preset: Preset, args: argparse.Namespace) -> Preset:
    if not (args.ad or args.ad_text):
        if args.ad_at or args.ad_mode or args.ad_duration is not None or args.ad_position:
            raise ConfigError("--ad-at/--ad-mode/--ad-duration/--ad-position требуют --ad или --ad-text")
        return preset
    return replace(preset, ads=AdSettings(enabled=True, slots=ad_slots_from_args(args)))


def apply_args(preset: Preset, args: argparse.Namespace) -> Preset:
    for step in (_apply_output_batch, _apply_music, _apply_subtitles, _apply_ads):
        preset = step(preset, args)
    return preset


# ------------------------------------------------------------------- running


def console_callbacks() -> BatchCallbacks:
    def on_overall(done: int, total: int, eta: float | None) -> None:
        sys.stderr.write(f"\r[{done}/{total}] ETA {format_eta(eta)}   ")
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
