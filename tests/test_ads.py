import random
from dataclasses import replace
from pathlib import Path

import pytest

from core.config import AdSettings, AdSlot, MetadataSettings, Preset
from core.ffmpeg import Capabilities, FeatureUnavailable, FFmpegTools, MediaInfo
from core.filtergraph import FilterGraph
from core.pipeline import JobContext, plan_job
from core.timeline import build_timeline
from features import ads as ads_mod
from features.ads import (
    AdError,
    OverlayAd,
    apply_overlays,
    banner_filter,
    overlay_source_filters,
    pick_file,
    position_xy,
    roll_clips,
    roll_overlays,
)
from features.metadata import roll_metadata

ALL_FILTERS = frozenset({"ass", "drawtext", "chromakey"})


def _identity(t):
    return t


def _ads(*slots):
    return AdSettings(enabled=True, slots=tuple(slots))


def _fake_clip(monkeypatch, duration=2.0):
    info = MediaInfo(path="promo.mp4", duration=duration, has_video=True, width=640, height=360,
                     has_audio=True)
    monkeypatch.setattr(ads_mod, "media_info", lambda tools, path: replace(info, path=path))
    return info


@pytest.mark.parametrize("position, expected", [
    ("top-left", ("80", "80")),
    ("bottom-right", ("W-w-80", "H-h-80")),
    ("center", ("(W-w)/2", "(H-h)/2")),
    ("top", ("(W-w)/2", "80")),
])
def test_position_expressions(position, expected):
    assert position_xy(position, 80, "w", "h") == expected


def test_pick_file_from_folder_and_missing_folder(tmp_path):
    for name in ("b.mp4", "a.MOV", "readme.txt", ".hidden.mp4"):
        (tmp_path / name).write_bytes(b"x")

    picked = pick_file(str(tmp_path), ads_mod.VIDEO_EXTENSIONS, random.Random(3))

    assert picked.name in {"a.MOV", "b.mp4"}
    with pytest.raises(AdError, match="Рекламный файл не найден"):
        pick_file(str(tmp_path / "nope"), ads_mod.VIDEO_EXTENSIONS, random.Random(3))


def test_roll_clips_resolves_percent_against_ad_free_duration(tmp_path, monkeypatch):
    (tmp_path / "promo.mp4").write_bytes(b"x")
    _fake_clip(monkeypatch)
    settings = _ads(AdSlot(mode="clip", path=str(tmp_path), at="50%"),
                    AdSlot(mode="overlay", text="hi"))

    clips = roll_clips(None, settings, random.Random(1), 10.0)

    assert len(clips) == 1
    assert clips[0].at == pytest.approx(5.0)
    assert clips[0].note == "clip:promo.mp4@5.0s"


def test_disabled_ads_roll_nothing(monkeypatch):
    settings = replace(_ads(AdSlot(mode="clip", path="missing")), enabled=False)

    assert roll_clips(None, settings, random.Random(1), 10.0) == []


def test_overlay_stops_where_a_spliced_clip_starts():
    timeline = build_timeline(0.0, 10.0, [(4.0, 2.0)], speed=1.0)  # ad occupies 4-6 s
    settings = _ads(AdSlot(mode="overlay", text="SALE", at="3", duration=5.0))

    [banner] = roll_overlays(settings, random.Random(1), timeline, _identity, 10.0)

    assert (banner.kind, banner.start, banner.end) == ("text", 3.0, 4.0)


def test_overlay_at_insertion_point_lands_after_the_clip():
    timeline = build_timeline(0.0, 10.0, [(4.0, 2.0)], speed=1.0)
    settings = _ads(AdSlot(mode="overlay", text="SALE", at="4", duration=3.0))

    [banner] = roll_overlays(settings, random.Random(1), timeline, _identity, 10.0)

    assert (banner.start, banner.end) == (6.0, 9.0)


def test_video_overlay_filters_key_scale_fade_and_shift():
    slot = AdSlot(mode="overlay", path="x.mp4", scale=0.5, fade=0.4, chroma_key="#00FF00")
    ov = OverlayAd(slot, "video", "x.mp4", start=2.0, end=5.0)

    filters = overlay_source_filters(ov, 1080)

    assert filters[0] == "chromakey=0x00FF00:0.15:0.05"
    assert "scale=540:-2:flags=bicubic" in filters
    assert "fade=t=out:st=2.6:d=0.4:alpha=1" in filters
    assert filters[-1] == "setpts=PTS-STARTPTS+2/TB"


def test_banner_reads_text_from_file_and_is_time_gated():
    ov = OverlayAd(AdSlot(mode="overlay", text="50% OFF: today"), "text", "", 1.0, 3.0)

    text = banner_filter(ov, "banner_0.txt", None)

    assert "textfile=banner_0.txt:expansion=none" in text
    assert "font=Sans" in text
    assert "enable='between(t,1,3)'" in text
    assert "50%" not in text  # the user text never enters the filter string


def test_apply_overlays_chains_image_and_writes_banner_file(tmp_path):
    graph, inputs = FilterGraph(), []
    image = OverlayAd(AdSlot(mode="overlay", path="logo.png", position="top"), "image", "logo.png", 1.0, 2.0)
    banner = OverlayAd(AdSlot(mode="overlay", text="Привет"), "text", "", 1.0, 2.0)

    def add_input(*args):
        inputs.append(args)
        return len(inputs)

    out = apply_overlays(graph, add_input, "v0", [image, banner], 1080, "30", tmp_path)
    rendered = graph.render()

    assert inputs == [("-loop", "1", "-framerate", "30", "-t", "1", "-i", "logo.png")]
    assert "[1:v:0]" in rendered and "overlay=x=(W-w)/2:y=80:eof_action=pass" in rendered
    assert (tmp_path / "banner_1.txt").read_text(encoding="utf-8") == "Привет"
    assert rendered.endswith(f"[{out}]")


def _ctx(tmp_path, ads, filters=ALL_FILTERS):
    caps = Capabilities(version="7", filters=filters, encoders=frozenset({"libx264"}), bsfs=frozenset())
    return JobContext(FFmpegTools("ffmpeg", "ffprobe"), caps, replace(Preset(), ads=ads), "libx264", tmp_path)


def test_plan_job_splices_clip_and_grows_duration(tmp_path, monkeypatch):
    (tmp_path / "promo.mp4").write_bytes(b"x")
    _fake_clip(monkeypatch, duration=2.0)
    source = MediaInfo(path="src.mp4", duration=8.0, has_video=True, width=720, height=1280,
                       has_audio=True)
    rng = random.Random(5)
    meta = roll_metadata(MetadataSettings(), random.Random(5))
    plain = plan_job(_ctx(tmp_path, AdSettings()), source, random.Random(5), tmp_path / "a.mp4", meta)

    plan = plan_job(_ctx(tmp_path, _ads(AdSlot(mode="clip", path=str(tmp_path), at="2"))),
                    source, rng, tmp_path / "b.mp4", meta)
    graph = plan.command[plan.command.index("-filter_complex") + 1]

    assert plan.expected_duration == pytest.approx(plain.expected_duration + 2.0)
    assert "concat=n=3:v=1:a=1" in graph
    assert "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black" in graph
    assert plan.notes["ads"].startswith("clip:promo.mp4@2.0s")


def test_plan_job_requires_drawtext_for_banners(tmp_path):
    source = MediaInfo(path="src.mp4", duration=8.0, has_video=True, width=720, height=1280)
    ctx = _ctx(tmp_path, _ads(AdSlot(mode="overlay", text="hi", at="1")), filters=frozenset())

    with pytest.raises(FeatureUnavailable, match="drawtext"):
        plan_job(ctx, source, random.Random(1), Path("o.mp4"),
                 roll_metadata(MetadataSettings(), random.Random(1)))
