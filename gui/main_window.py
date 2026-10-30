"""Main window: tabs for every preset section, preset menu and persisted state."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QByteArray, QSettings, QTimer
from PySide6.QtWidgets import QFileDialog, QMainWindow, QMessageBox, QTabWidget, QWidget

from core.config import (
    ConfigError,
    Preset,
    load_preset,
    preset_from_dict,
    preset_to_dict,
    save_preset,
)
from core.ffmpeg import FFmpegNotFound, locate_tools
from gui.ads_tab import AdsTab
from gui.files_tab import FilesTab
from gui.forms import SectionForm, metadata_groups, music_groups, subtitle_groups, uniq_groups
from gui.queue_tab import JobRequest, QueueTab

APP_TITLE = "Shorts Uniq"
PRESET_FILTER = "Пресеты (*.json);;Все файлы (*)"
KEY_PRESET, KEY_FILES, KEY_GEOMETRY = "preset_json", "files", "geometry"
DEFAULT_SIZE = (1100, 800)


class MainWindow(QMainWindow):
    def __init__(self, settings: QSettings | None = None, check_tools: bool = True,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = settings or QSettings("shorts-uniq", "shorts-uniq")
        self.setWindowTitle(APP_TITLE)
        self.resize(*DEFAULT_SIZE)
        self._base = Preset()  # keeps preset fields that no tab edits

        self.files = FilesTab()
        self.uniq = SectionForm(uniq_groups())
        self.meta = SectionForm(metadata_groups())
        self.music = SectionForm(music_groups())
        self.subs = SectionForm(subtitle_groups())
        self.ads = AdsTab()
        self.queue = QueueTab(self.request_job)
        self._editors = (self.files, self.uniq, self.meta, self.music, self.subs, self.ads,
                         self.queue)

        tabs = QTabWidget()
        for widget, title in ((self.files, "Файлы"), (self.uniq, "Уникализация"),
                              (self.meta, "Метаданные"), (self.music, "Музыка"),
                              (self.subs, "Субтитры"), (self.ads, "Реклама"),
                              (self.queue, "Очередь")):
            tabs.addTab(widget, title)
        self.setCentralWidget(tabs)
        self._build_menu()
        self._restore_state()
        if check_tools:
            QTimer.singleShot(0, self._check_tools)

    # ---------------------------------------------------------------- preset

    def load_preset(self, preset: Preset) -> None:
        self._base = preset
        for editor in self._editors:
            editor.load(preset)

    def current_preset(self) -> Preset:
        preset = self._base
        for editor in self._editors:
            preset = editor.apply(preset)
        return preset

    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu("Пресет")
        menu.addAction("Загрузить…", self._open_preset)
        menu.addAction("Сохранить как…", self._save_preset)
        menu.addSeparator()
        menu.addAction("Сбросить", lambda: self.load_preset(Preset()))

    def _open_preset(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Загрузить пресет", "", PRESET_FILTER)
        if not path:
            return
        try:
            self.load_preset(load_preset(path))
        except (ConfigError, OSError, ValueError) as exc:
            self.show_error(f"Не удалось загрузить пресет:\n{exc}")

    def _save_preset(self) -> None:
        try:
            preset = self.current_preset()
        except ConfigError as exc:
            self.show_error(str(exc))
            return
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить пресет", "preset.json",
                                              PRESET_FILTER)
        if not path:
            return
        try:
            save_preset(preset, path)
        except OSError as exc:
            self.show_error(f"Не удалось сохранить пресет:\n{exc}")

    # ------------------------------------------------------------------ jobs

    def show_error(self, message: str) -> None:
        QMessageBox.warning(self, APP_TITLE, message)

    def request_job(self, preview: bool) -> JobRequest | None:
        try:
            preset = self.current_preset()
        except ConfigError as exc:
            self.show_error(str(exc))
            return None
        if not preset.output.output_dir:
            self.show_error("Укажите папку результата на вкладке «Файлы».")
            return None
        sources = self.files.sources(preset.output.recursive)
        if not sources:
            self.show_error("Нет видео для обработки: добавьте файлы или папки на вкладке «Файлы».")
            return None
        if preview:
            sources = sources[:1]
        try:
            tools = locate_tools(preset.batch.ffmpeg_path)
        except FFmpegNotFound as exc:
            self.show_error(str(exc))
            return None
        return tools, preset, sources, Path(preset.output.output_dir)

    def _check_tools(self) -> None:
        try:
            locate_tools(self.queue.form.field("batch", "ffmpeg_path").get())
        except FFmpegNotFound as exc:
            self.show_error(str(exc))

    # ------------------------------------------------------------ persistence

    def _restore_state(self) -> None:
        geometry = self.settings.value(KEY_GEOMETRY)
        if isinstance(geometry, QByteArray):
            self.restoreGeometry(geometry)
        preset = Preset()
        raw = self.settings.value(KEY_PRESET)
        if raw:
            try:
                preset = preset_from_dict(json.loads(raw))
            except (ConfigError, ValueError, TypeError):
                preset = Preset()
        self.load_preset(preset)
        files = self.settings.value(KEY_FILES) or []
        self.files.add_paths([files] if isinstance(files, str) else list(files))

    def _save_state(self) -> None:
        self.settings.setValue(KEY_GEOMETRY, self.saveGeometry())
        self.settings.setValue(KEY_FILES, self.files.paths())
        try:
            preset = self.current_preset()
        except ConfigError:
            preset = None  # keep the last valid preset rather than storing a broken one
        if preset is not None:
            self.settings.setValue(KEY_PRESET, json.dumps(preset_to_dict(preset),
                                                          ensure_ascii=False))
        self.settings.sync()

    def closeEvent(self, event) -> None:
        if self.queue.running:
            answer = QMessageBox.question(self, APP_TITLE,
                                          "Обработка ещё идёт. Остановить и выйти?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.queue.stop_and_wait()
        self._save_state()
        super().closeEvent(event)
