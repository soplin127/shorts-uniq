import random
from datetime import date

import pytest

import features.metadata as md
from core.config import ConfigError, MetadataSettings


def test_creation_time_falls_inside_configured_dates():
    settings = MetadataSettings(date_from="2025-03-01", date_to="2025-03-31")
    rng = random.Random(3)

    for _ in range(100):
        params = md.roll_metadata(settings, rng)
        assert "20250301" <= params.date_compact <= "20250331"


def test_default_date_range_is_last_year():
    start, end = md.date_range(MetadataSettings(), today=date(2026, 6, 1))

    assert (start, end) == (date(2025, 6, 1), date(2026, 6, 1))


def test_reversed_dates_are_rejected():
    with pytest.raises(ConfigError):
        md.date_range(MetadataSettings(date_from="2026-01-02", date_to="2026-01-01"))


def test_neutral_title_mode_picks_from_list():
    settings = MetadataSettings(title_mode="neutral", neutral_titles=("clip",))

    assert md.roll_metadata(settings, random.Random(1)).title == "clip"


def test_metadata_args_strip_inherited_and_tool_tags():
    params = md.roll_metadata(MetadataSettings(), random.Random(1))

    args = md.metadata_args(params)

    assert args[:4] == ["-map_metadata", "-1", "-map_chapters", "-1"]
    assert "+bitexact" in args
    assert ["-metadata:s:v:0", "encoder="] == args[args.index("-metadata:s:v:0"):][:2]


def test_structural_source_values_are_not_treated_as_leaks():
    tags = {
        "format.major_brand": "isom",
        "format.compatible_brands": "isomiso2avc1mp41",
        "stream0.handler_name": "VideoHandler",
        "format.title": "SECRET_TITLE",
        "stream0.encoder": "Lavc libx264",
        "format.x": "ab",  # too short to be meaningful
    }

    assert md._source_values(tags) == {"SECRET_TITLE", "Lavc libx264"}


def test_signature_scan_finds_strings_across_chunk_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr(md, "SCAN_CHUNK_BYTES", 8)
    path = tmp_path / "f.bin"
    path.write_bytes(b"\x00" * 6 + b"Lavc63.1" + b"\x00" * 20 + b"x264 - core 164")

    assert md.find_signatures(str(path)) == ["Lavc", "x264 - core"]


def test_signature_scan_on_clean_file(tmp_path):
    path = tmp_path / "clean.bin"
    path.write_bytes(b"\x00\x01 just bytes " * 1000)

    assert md.find_signatures(str(path)) == []
