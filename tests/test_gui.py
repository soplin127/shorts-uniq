"""GUI tests: run headless with the offscreen Qt platform (skipped without PySide6)."""
import os
import subprocess
from dataclasses import replace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QEventLoop, QSettings, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.config import (  # noqa: E402
    AdSettings,
    AdSlot,
    BatchSettings,
    ConfigError,
    EncoderSettings,
    MetadataSettings,
    MusicSettings,
    OutputSettings,
    Preset,
    Range,
    SubtitleSettings,
    UniqSettings,
)
from core.ffmpeg import FFmpegNotFound, locate_tools  # noqa: E402
from gui.ads_tab import AdsTab  # noqa: E402
from gui.fields import ListField, RangeField, RangeSlider  # noqa: E402
from gui.files_tab import FilesTab  # noqa: E402
from gui.forms import (  # noqa: E402
    SectionForm,
    batch_groups,
    metadata_groups,
    music_groups,
    output_groups,
    subtitle_groups,
    uniq_groups,
)
from gui.main_window import MainWindow  # noqa: E402
from gui.queue_tab import QueueTab  # noqa: E402

ALL_GROUPS = (uniq_groups, metadata_groups, music_groups, subtitle_groups, output_groups,
              batch_groups)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _round_trip(preset):
    forms = [SectionForm(groups()) for groups in ALL_GROUPS]
    for form in forms:
        form.load(preset)
    result = Preset()
    for form in forms:
        result = form.apply(result)
    return result


def test_forms_round_trip_default_preset(qapp):
    assert _round_trip(Preset()) == Preset()


def test_forms_round_trip_custom_values(qapp):
    preset = replace(
        Preset(),
        uniq=UniqSettings(scale_pct=Range(3.5, 9.0), fps_choices=(25.0, 29.97), flip_enabled=True),
        encoder=EncoderSettings(crf=Range(20.0, 26.0), preset="slow", hwaccel="videotoolbox"),
        metadata=MetadataSettings(date_from="2026-01-01", title_mode="neutral",
                                  neutral_titles=("my clip", "IMG")),
        music=MusicSettings(enabled=True, path="/m/track.mp3", volume_db=-18.5, ducking=False),
        subtitles=SubtitleSettings(enabled=True, source="auto", position="top",
                                   highlight_color="#00FF00", max_words=2),
        output=OutputSettings(output_dir="/out", variants=4, recursive=True),
        batch=BatchSettings(workers=3, seed="abc", skip_existing=False),
    )

    assert _round_trip(preset) == preset


def test_form_apply_rejects_invalid_values(qapp):
    form = SectionForm(uniq_groups())
    form.load(Preset())
    form.field("uniq", "target_width").set(1081)

    with pytest.raises(ConfigError, match="Разрешение"):
        form.apply(Preset())


def test_list_field_accepts_comma_decimals_and_rejects_garbage(qapp):
    field = ListField(float)
    field.widget.setText("30; 29,97")

    assert field.get() == (30.0, 29.97)
    field.widget.setText("30; abc")
    with pytest.raises(ConfigError, match="Неверный список"):
        field.get()


def test_range_slider_clamps_and_orders_values(qapp):
    slider = RangeSlider(0.0, 10.0)

    slider.setValues(12.0, -3.0)
    assert slider.values() == (0.0, 10.0)
    slider.setValues(7.0, 2.0)
    assert slider.values() == (2.0, 7.0)


def test_range_field_keeps_min_not_above_max(qapp):
    field = RangeField(0.0, 10.0)
    field.set(Range(2.0, 5.0))

    field.min_spin.setValue(8.0)

    assert field.get() == Range(8.0, 8.0)
    assert field.slider.values() == (8.0, 8.0)


def test_range_field_widens_bounds_for_out_of_range_preset(qapp):
    field = RangeField(0.0, 10.0)

    field.set(Range(-5.0, 50.0))

    assert field.get() == Range(-5.0, 50.0)


def _ads_preset():
    return replace(Preset(), ads=AdSettings(enabled=True, slots=(
        AdSlot(path="promo.mp4", at="7"),
        AdSlot(mode="overlay", text="SALE", position="top-right", duration=3.0),
    )))


def test_ads_tab_round_trip(qapp):
    tab = AdsTab()
    tab.load(_ads_preset())

    assert tab.apply(Preset()).ads == _ads_preset().ads


def test_ads_tab_edit_replaces_selected_slot(qapp):
    tab = AdsTab()
    tab.load(_ads_preset())
    tab.select(1)

    tab.editor.field("", "at").set("40%")

    slots = tab.apply(Preset()).ads.slots
    assert slots[1].at == "40%"
    assert slots[1].text == "SALE"
    assert slots[0] == _ads_preset().ads.slots[0]


def test_ads_tab_invalid_edit_keeps_slot_and_shows_error(qapp):
    tab = AdsTab()
    tab.load(_ads_preset())
    tab.select(0)

    tab.editor.field("", "path").set("")

    assert tab.error_text()
    assert tab.apply(Preset()).ads.slots[0].path == "promo.mp4"


def test_ads_tab_add_banner_and_remove(qapp):
    tab = AdsTab()
    tab.load(Preset())

    tab.add_slot(AdSlot(mode="overlay", text="Hi"))
    tab.add_slot(AdSlot(path="a.mp4"))
    tab.select(0)
    tab.remove_selected()

    assert [s.path for s in tab.apply(Preset()).ads.slots] == ["a.mp4"]


def test_files_tab_dedups_paths_and_collects_videos(qapp, tmp_path):
    video = tmp_path / "a.mp4"
    video.write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("x")
    tab = FilesTab()

    tab.add_paths([str(video), str(video), str(tmp_path)])

    assert tab.paths() == [str(video), str(tmp_path)]
    assert tab.sources(recursive=False) == [video.resolve()]


def _settings(tmp_path):
    return QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)


def test_main_window_collects_preset_and_restores_it(qapp, tmp_path):
    preset = replace(Preset(), music=MusicSettings(enabled=True, path="m.mp3"),
                     ads=_ads_preset().ads)
    window = MainWindow(settings=_settings(tmp_path), check_tools=False)
    window.load_preset(preset)
    window.files.add_paths([str(tmp_path)])

    assert window.current_preset() == preset
    window.close()

    restored = MainWindow(settings=_settings(tmp_path), check_tools=False)
    assert restored.current_preset() == preset
    assert restored.files.paths() == [str(tmp_path)]
    restored.close()


def test_main_window_job_request_requires_output_folder(qapp, tmp_path, monkeypatch):
    errors = []
    window = MainWindow(settings=_settings(tmp_path), check_tools=False)
    monkeypatch.setattr(window, "show_error", errors.append)
    window.files.add_paths([str(tmp_path)])

    assert window.request_job(preview=False) is None
    assert "папку результата" in errors[-1]
    window.close()


# ------------------------------------------------------------------ end to end


@pytest.fixture(scope="module")
def tools():
    try:
        return locate_tools()
    except FFmpegNotFound:
        pytest.skip("ffmpeg is not installed")


def _wait(signal, timeout_ms):
    loop = QEventLoop()
    signal.connect(loop.quit)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()


def test_queue_tab_runs_batch_and_reports_progress(qapp, tools, tmp_path):
    src = tmp_path / "clip.mp4"
    subprocess.run([tools.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=s=360x640:r=30:d=2", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    preset = replace(Preset(), encoder=EncoderSettings(hwaccel="none", preset="ultrafast"),
                     output=OutputSettings(variants=2), batch=BatchSettings(workers=2, seed="g"))
    tab = QueueTab(lambda preview: (tools, preset, [src], tmp_path / "out"))
    tab.load(preset)
    finished = []
    tab.batch_finished.connect(finished.append)

    tab.start(preview=False)
    _wait(tab.batch_finished, 60_000)

    assert finished, "batch did not finish in time"
    assert [r.status for r in finished[0]] == ["done", "done"]
    assert tab.status_text(0) == "Готово" and tab.status_text(1) == "Готово"
    assert tab.overall_value() == (2, 2)
    assert len(list((tmp_path / "out").glob("*.mp4"))) == 2
