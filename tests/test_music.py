import random
from pathlib import Path

import pytest

from core.config import MusicSettings
from core.filtergraph import FilterGraph
from features.music import MusicError, MusicParams, list_tracks, mix_music, music_filters, pick_track, roll_music


def _params(**overrides):
    base = dict(track="song.mp3", start=0.0, duration=10.0, fade_in=1.0, fade_out=1.5)
    return MusicParams(**{**base, **overrides})


def test_pick_track_from_folder_ignores_non_audio_and_hidden(tmp_path):
    for name in ("b.mp3", "a.WAV", "notes.txt", ".hidden.mp3"):
        (tmp_path / name).write_bytes(b"x")

    assert [p.name for p in list_tracks(str(tmp_path))] == ["a.WAV", "b.mp3"]
    assert pick_track(str(tmp_path), random.Random(1)).name in {"a.WAV", "b.mp3"}


def test_pick_track_raises_when_nothing_found(tmp_path):
    with pytest.raises(MusicError, match="Музыка не найдена"):
        pick_track(str(tmp_path), random.Random(1))


def test_random_start_leaves_room_for_the_whole_clip():
    rng = random.Random(7)

    starts = [roll_music(MusicSettings(), rng, Path("t.mp3"), 60.0, 15.0).start for _ in range(100)]

    assert all(0.0 <= s <= 45.0 for s in starts)
    assert len(set(starts)) > 1


def test_short_track_starts_at_zero_and_fades_fit_the_clip():
    settings = MusicSettings(fade_in=5.0, fade_out=5.0)

    params = roll_music(settings, random.Random(1), Path("t.mp3"), 3.0, 4.0)

    assert params.start == 0.0
    assert params.fade_in == params.fade_out == pytest.approx(2.0)


def test_music_filters_trim_fade_and_set_volume():
    filters = music_filters(MusicSettings(volume_db=-12.0), _params())

    assert "atrim=duration=10" in filters
    assert "volume=-12dB" in filters
    assert "afade=t=out:st=8.5:d=1.5" in filters


def test_ducking_mix_uses_sidechain_and_keeps_original_length():
    graph = FilterGraph()

    out = mix_music(graph, "a0", 3, MusicSettings(ducking=True), _params())
    rendered = graph.render()

    assert "[3:a:0]" in rendered
    assert "asplit=2" in rendered and "sidechaincompress" in rendered
    assert "amix=inputs=2:duration=first:normalize=0" in rendered
    assert rendered.endswith(f"[{out}]")


def test_replace_mode_discards_original_audio():
    graph = FilterGraph()

    out = mix_music(graph, "a0", 1, MusicSettings(replace_audio=True), _params())
    rendered = graph.render()

    assert "[a0]anullsink" in rendered
    assert "amix" not in rendered
    assert out in rendered
