"""Ads tab: a list of ad slots (clips, overlays, banners) and an editor for the selected one."""
from __future__ import annotations

from dataclasses import replace

from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from core.config import AD_POSITIONS, AdSettings, AdSlot, ConfigError, Preset
from gui.fields import CheckField, ChoiceField, ColorField, FloatField, IntField, PathField, TextField
from gui.forms import FieldGroup, SectionForm

VIDEO_FILTER = "Видео (*.mp4 *.mov *.webm *.mkv);;Все файлы (*)"
OVERLAY_FILTER = "Картинки и видео (*.png *.jpg *.jpeg *.webp *.mp4 *.mov *.webm *.mkv);;Все файлы (*)"
AT_HINT = "7 | 00:07 | 35% | random:20%-60%"
MODE_LABELS = {"clip": "Вставка", "overlay": "Оверлей"}


def slot_groups() -> list[FieldGroup]:
    return [
        FieldGroup("Слот", "", [
            ("mode", "Режим", ChoiceField([("clip", "Вставка ролика"), ("overlay", "Поверх видео")])),
            ("path", "Файл или папка", PathField(("file", "dir"), OVERLAY_FILTER)),
            ("text", "Текст баннера", TextField("если файл не указан")),
            ("at", "Когда", TextField(AT_HINT)),
        ]),
        FieldGroup("Оверлей", "", [
            ("duration", "Длительность", FloatField(0.1, 120.0, 0.5, 1, " с")),
            ("position", "Позиция", ChoiceField(AD_POSITIONS)),
            ("scale", "Ширина от кадра", FloatField(0.05, 1.0, 0.05, 2)),
            ("margin", "Отступ", IntField(0, 500)),
            ("fade", "Появление/исчезание", FloatField(0.0, 5.0, 0.1, 1, " с")),
            ("chroma_key", "Хромакей", ColorField(allow_empty=True)),
            ("chroma_similarity", "Допуск хромакея", FloatField(0.01, 1.0, 0.01, 2)),
        ]),
        FieldGroup("Баннер", "", [
            ("text_size", "Размер текста", IntField(8, 300)),
            ("text_color", "Цвет текста", ColorField()),
            ("box_color", "Цвет подложки", ColorField()),
        ]),
    ]


def describe(slot: AdSlot) -> str:
    what = slot.path or f"«{slot.text}»"
    return f"{MODE_LABELS[slot.mode]}: {what} @ {slot.at}"


class AdsTab(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._slots: tuple[AdSlot, ...] = ()
        self._loading = False
        self.enabled = CheckField("Вставлять рекламу")
        self.list = QListWidget()
        self.editor = SectionForm(slot_groups())
        self.error = QLabel()
        self.error.setStyleSheet("color: #c0392b")
        self.error.setWordWrap(True)

        buttons = QHBoxLayout()
        for text, slot in (("Добавить ролик…", self._add_clip),
                           ("Добавить оверлей…", self._add_overlay),
                           ("Добавить баннер…", self._add_banner),
                           ("Удалить", self.remove_selected)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.list)
        left_layout.addLayout(buttons)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self.editor)
        right_layout.addWidget(self.error)
        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(self.enabled.widget)
        layout.addWidget(splitter)

        self.list.currentRowChanged.connect(self._show_slot)
        self.editor.changed.connect(self._edit_slot)
        self._show_slot(-1)

    # ----------------------------------------------------------- preset I/O

    def load(self, preset: Preset) -> None:
        self.enabled.set(preset.ads.enabled)
        self._slots = preset.ads.slots
        self._refresh_list(0 if self._slots else -1)

    def apply(self, preset: Preset) -> Preset:
        return replace(preset, ads=AdSettings(enabled=self.enabled.get(), slots=self._slots))

    # -------------------------------------------------------------- editing

    def select(self, row: int) -> None:
        self.list.setCurrentRow(row)

    def error_text(self) -> str:
        return self.error.text()

    def add_slot(self, slot: AdSlot) -> None:
        self._slots = (*self._slots, slot)
        self.enabled.set(True)
        self._refresh_list(len(self._slots) - 1)

    def remove_selected(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        self._slots = self._slots[:row] + self._slots[row + 1:]
        self._refresh_list(min(row, len(self._slots) - 1))

    def _refresh_list(self, row: int) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        self.list.addItems([describe(s) for s in self._slots])
        self.list.blockSignals(False)
        self.list.setCurrentRow(row)
        self._show_slot(row)

    def _show_slot(self, row: int) -> None:
        self.error.clear()
        self.editor.setEnabled(row >= 0)
        if row < 0:
            return
        self._loading = True
        self.editor.load(self._slots[row])
        self._loading = False

    def _edit_slot(self) -> None:
        row = self.list.currentRow()
        if self._loading or row < 0:
            return
        try:
            values = {}
            for group in self.editor.groups:
                values.update(group.values())
            slot = AdSlot(**values)
        except ConfigError as exc:
            self.error.setText(f"Слот не сохранён: {exc}")
            return
        self.error.clear()
        self._slots = (*self._slots[:row], slot, *self._slots[row + 1:])
        self.list.item(row).setText(describe(slot))

    # -------------------------------------------------------------- dialogs

    def _add_clip(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Рекламный ролик", "", VIDEO_FILTER)
        if path:
            self.add_slot(AdSlot(path=path))

    def _add_overlay(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Картинка или видео поверх", "", OVERLAY_FILTER)
        if path:
            self.add_slot(AdSlot(mode="overlay", path=path))

    def _add_banner(self) -> None:
        text, ok = QInputDialog.getText(self, "Текстовый баннер", "Текст:")
        if ok and text.strip():
            self.add_slot(AdSlot(mode="overlay", text=text.strip()))
