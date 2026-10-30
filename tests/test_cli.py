import pytest

import cli
from core.config import AdSettings, AdSlot, ConfigError, Preset


def _apply(*argv, preset=None):
    args = cli.build_parser().parse_args(["-i", "in", "-o", "out", *argv])
    return cli.apply_args(preset or Preset(), args)


def test_readme_example_enables_music_subs_and_ad():
    preset = _apply("--variants", "3", "--music", "./music", "--subs", "auto",
                    "--ad", "./ads/promo.mp4", "--ad-at", "7")

    assert preset.output.variants == 3
    assert (preset.music.enabled, preset.music.path) == (True, "./music")
    assert (preset.subtitles.enabled, preset.subtitles.source) == (True, "auto")
    assert preset.ads.enabled
    assert preset.ads.slots == (AdSlot(mode="clip", path="./ads/promo.mp4", at="7"),)


def test_music_flags_override_preset_values():
    preset = _apply("--music", "t.mp3", "--music-volume", "-20", "--replace-audio", "--no-duck")

    assert preset.music.volume_db == -20
    assert preset.music.replace_audio and not preset.music.ducking


def test_subtitle_flags_keep_unspecified_preset_values():
    base = Preset()

    preset = _apply("--subs", "file", "--max-words", "2", "--highlight", "--lang", "ru")

    assert preset.subtitles.max_words == 2
    assert preset.subtitles.highlight_current_word
    assert preset.subtitles.language == "ru"
    assert preset.subtitles.whisper_model == base.subtitles.whisper_model
    assert not preset.subtitles.save_srt


def test_subs_none_disables_preset_subtitles():
    on = _apply("--subs", "auto")

    assert not _apply("--subs", "none", preset=on).subtitles.enabled


def test_ad_values_pair_by_index_and_last_value_repeats():
    preset = _apply("--ad", "a.mp4", "--ad", "logo.png", "--ad-text", "SALE",
                    "--ad-at", "3", "--ad-at", "40%",
                    "--ad-mode", "clip", "--ad-mode", "overlay",
                    "--ad-position", "top-right", "--ad-duration", "4")

    clip, logo, banner = preset.ads.slots
    assert (clip.mode, clip.at) == ("clip", "3")
    assert (logo.mode, logo.at, logo.position, logo.duration) == ("overlay", "40%", "top-right", 4.0)
    assert (banner.mode, banner.text, banner.at) == ("overlay", "SALE", "40%")


def test_cli_ads_replace_preset_slots():
    base = Preset(ads=AdSettings(enabled=True, slots=(AdSlot(path="old.mp4"),)))

    preset = _apply("--ad", "new.mp4", preset=base)

    assert [s.path for s in preset.ads.slots] == ["new.mp4"]


def test_preset_ads_untouched_without_ad_flags():
    base = Preset(ads=AdSettings(enabled=True, slots=(AdSlot(path="old.mp4"),)))

    assert _apply(preset=base).ads == base.ads


def test_ad_options_without_ad_are_rejected():
    with pytest.raises(ConfigError, match="--ad"):
        _apply("--ad-at", "5")


def test_invalid_ad_position_is_rejected_by_parser():
    with pytest.raises(SystemExit):
        _apply("--ad", "a.mp4", "--ad-position", "middle")
