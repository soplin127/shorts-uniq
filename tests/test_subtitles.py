from pathlib import Path

import pytest

from core.config import SubtitleSettings
from core.timeline import build_timeline
from features import transcribe as transcribe_mod
from features.subtitles import (
    Cue,
    Word,
    ass_color,
    ass_timestamp,
    build_events,
    find_sidecar,
    load_sidecar,
    parse_ass,
    parse_srt,
    render_ass,
    render_srt,
    srt_timestamp,
)

SRT = """1
00:00:01,000 --> 00:00:03,000
<i>Привет</i> всем

2
00:00:04.500 --> 00:00:06,000
Как дела? Отлично
"""

ASS = """[Script Info]
Title: x

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:02.00,0:00:04.00,Default,,0,0,0,,{\\b1}Hello,\\Nworld
"""


def _cue(start, end, *texts):
    step = (end - start) / len(texts)
    return Cue(start, end, tuple(Word(start + i * step, start + (i + 1) * step, t)
                                 for i, t in enumerate(texts)))


def test_parse_srt_strips_tags_and_accepts_dot_separator():
    cues = parse_srt(SRT)

    assert [c.text for c in cues] == ["Привет всем", "Как дела? Отлично"]
    assert cues[1].start == pytest.approx(4.5)
    assert cues[0].words[0].start == pytest.approx(1.0)
    assert cues[0].words[-1].end == pytest.approx(3.0)


def test_parse_ass_keeps_commas_in_text_and_drops_overrides():
    cues = parse_ass(ASS)

    assert len(cues) == 1
    assert cues[0].text == "Hello, world"
    assert (cues[0].start, cues[0].end) == (2.0, 4.0)


def test_sidecar_found_case_insensitively_and_cp1251_decoded(tmp_path):
    video = tmp_path / "Clip.mp4"
    video.write_bytes(b"")
    (tmp_path / "clip.SRT").write_bytes(SRT.encode("cp1251"))

    sidecar = find_sidecar(video)

    assert sidecar is not None
    assert load_sidecar(sidecar)[0].text == "Привет всем"


def test_shorts_chunks_respect_max_words_and_sentence_ends():
    cues = [_cue(0.0, 6.0, "one", "two", "three.", "four", "five", "six")]
    settings = SubtitleSettings(shorts_style=True, max_words=2)

    events = build_events(cues, build_timeline(0.0, 10.0, [], 1.0), settings)

    assert [e.text for e in events] == ["one two", "three.", "four five", "six"]
    assert all(a.end <= b.start + 1e-9 for a, b in zip(events, events[1:]))


def test_events_follow_trim_speed_and_ad_insertion():
    cues = [_cue(1.0, 2.0, "trimmed"), _cue(4.0, 5.0, "late")]
    timeline = build_timeline(2.0, 10.0, [(3.0, 2.0)], speed=2.0)  # ad of 2 s at source 3 s

    events = build_events(cues, timeline, SubtitleSettings(max_words=3))

    assert [e.text for e in events] == ["late"]
    # source 2-3 s -> out 0-0.5 s, ad 0.5-2.5 s, then source 4 s -> 2.5 + (4-3)/2
    assert events[0].start == pytest.approx(3.0)


def test_highlight_emits_one_line_per_word_with_colours():
    settings = SubtitleSettings(highlight_current_word=True, highlight_color="#FFE000")
    event = _cue(0.0, 3.0, "a", "b", "c")

    ass = render_ass([event], settings, 1080, 1920)
    dialogues = [l for l in ass.splitlines() if l.startswith("Dialogue:")]

    assert len(dialogues) == 3
    assert "{\\c&H0000E0FF&}b" in dialogues[1]
    assert "PlayResY: 1920" in ass


def test_uppercase_srt_export_uses_output_times():
    settings = SubtitleSettings(uppercase=True)

    srt = render_srt([_cue(61.5, 62.0, "hi")], settings)

    assert srt == "1\n00:01:01,500 --> 00:01:02,000\nHI\n"


@pytest.mark.parametrize("seconds, ass, srt", [
    (0.0, "0:00:00.00", "00:00:00,000"),
    (3725.456, "1:02:05.46", "01:02:05,456"),
])
def test_timestamps(seconds, ass, srt):
    assert ass_timestamp(seconds) == ass
    assert srt_timestamp(seconds) == srt


def test_ass_color_converts_rgb_to_bgr_and_rejects_garbage():
    assert ass_color("#112233") == "&H00332211"
    with pytest.raises(ValueError):
        ass_color("red")


def test_transcription_is_cached_per_file(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"data")
    calls = []

    def fake_whisper(source, model, language):
        calls.append(source)
        return [_cue(0.0, 1.0, "hello")]

    monkeypatch.setattr(transcribe_mod, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(transcribe_mod, "_run_whisper", fake_whisper)

    first = transcribe_mod.transcribe(video, "small")
    second = transcribe_mod.transcribe(video, "small")

    assert first == second and len(calls) == 1
    assert first[0].words[0].text == "hello"


def test_missing_faster_whisper_gives_install_hint(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "faster_whisper":
            raise ImportError
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(transcribe_mod, "_models", {})

    with pytest.raises(Exception, match="pip install faster-whisper"):
        transcribe_mod._load_model("small")
