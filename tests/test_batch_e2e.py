"""End-to-end: real ffmpeg encodes of tiny generated clips (skipped without ffmpeg)."""
import subprocess
from dataclasses import replace

import pytest

from core.batch import BatchRunner
from core.config import BatchSettings, EncoderSettings, OutputSettings, Preset
from core.ffmpeg import FFmpegNotFound, locate_tools, probe
from features.metadata import find_signatures


@pytest.fixture(scope="module")
def tools():
    try:
        return locate_tools()
    except FFmpegNotFound:
        pytest.skip("ffmpeg is not installed")


def _make_clip(tools, path, *, size, seconds, audio, tags=()):
    cmd = [tools.ffmpeg, "-v", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=s={size}:r=30:d={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=f=440:d={seconds}"]
    for key, value in tags:
        cmd += ["-metadata", f"{key}={value}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    subprocess.run(cmd + [str(path)], check=True)


@pytest.fixture(scope="module")
def sources(tools, tmp_path_factory):
    folder = tmp_path_factory.mktemp("in")
    _make_clip(tools, folder / "tagged clip.mp4", size="360x640", seconds=3, audio=True,
               tags=[("title", "SECRET_TITLE"), ("artist", "Original Author")])
    _make_clip(tools, folder / "silent.mov", size="640x360", seconds=2, audio=False)
    return sorted(folder.iterdir())


def _preset(seed="t"):
    return replace(
        Preset(),
        encoder=EncoderSettings(hwaccel="none", preset="ultrafast"),
        output=OutputSettings(variants=2),
        batch=BatchSettings(workers=2, seed=seed),
    )


def test_batch_produces_clean_vertical_outputs_and_skips_on_rerun(tools, sources, tmp_path):
    runner = BatchRunner(tools, _preset(), sources, tmp_path)

    results = runner.run()

    assert [r.status for r in results] == ["done"] * 4, [r.error for r in results]
    outputs = sorted(tmp_path.glob("*.mp4"))
    assert len(outputs) == 4
    for out in outputs:
        info = probe(tools, out)
        assert (info.width, info.height) == (1080, 1920)
        assert info.has_audio
        assert not any("SECRET" in v or "Original" in v for v in info.tags.values())
        assert find_signatures(str(out)) == []
    assert len(list(tmp_path.glob("report_*.csv"))) == 1

    rerun = BatchRunner(tools, _preset(), sources, tmp_path).run()

    assert [r.status for r in rerun] == ["skipped"] * 4
    assert sorted(tmp_path.glob("*.mp4")) == outputs


def test_seeded_runs_pick_identical_names(tools, sources, tmp_path):
    first = BatchRunner(tools, _preset("same"), sources[:1], tmp_path / "a").run()
    second = BatchRunner(tools, _preset("same"), sources[:1], tmp_path / "b").run()

    assert [r.output.name for r in first] == [r.output.name for r in second]


def test_preview_encodes_one_short_file(tools, sources, tmp_path):
    preset = replace(_preset(), batch=BatchSettings(seed="p", preview_seconds=1.0))

    results = BatchRunner(tools, preset, sources[:1], tmp_path, preview=True).run()

    assert len(results) == 1 and results[0].status == "done"
    assert results[0].output.name.startswith("preview_")
    assert probe(tools, results[0].output).duration <= 1.2


def test_broken_source_fails_alone(tools, sources, tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")

    results = BatchRunner(tools, _preset(), [broken, sources[0]], tmp_path / "out").run()

    assert [r.status for r in results] == ["failed", "failed", "done", "done"]
