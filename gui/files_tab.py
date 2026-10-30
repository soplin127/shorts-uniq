"""Files tab: input files/folders (drag-and-drop) and output settings."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.config import Preset
from core.sources import collect_sources
from gui.forms import SectionForm, output_groups

VIDEO_FILTER = "Видео (*.mp4 *.mov *.webm *.mkv);;Все файлы (*)"


class DropList(QListWidget):
    """List that accepts files and folders dropped from the file manager."""

    def __init__(self, on_drop, parent: QWidget | None = None):
        super().__init__(parent)
        self.on_drop = on_drop
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.on_drop(paths)
            event.acceptProposedAction()


class FilesTab(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.list = DropList(self.add_paths)
        self.output = SectionForm(output_groups())
        self.count = QLabel()

        buttons = QHBoxLayout()
        for text, slot in (("Добавить файлы…", self._pick_files),
                           ("Добавить папку…", self._pick_folder),
                           ("Удалить выбранные", self._remove_selected),
                           ("Очистить", self.clear)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.count)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Перетащите сюда видео или папки:"))
        layout.addWidget(self.list, 2)
        layout.addLayout(buttons)
        layout.addWidget(self.output, 1)
        self._update_count()

    # ----------------------------------------------------------------- paths

    def paths(self) -> list[str]:
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())]

    def add_paths(self, paths) -> None:
        known = set(self.paths())
        for raw in paths:
            path = str(Path(raw))
            if path in known:
                continue
            known.add(path)
            self.list.addItem(path)
            self.list.item(self.list.count() - 1).setData(Qt.ItemDataRole.UserRole, path)
        self._update_count()

    def clear(self) -> None:
        self.list.clear()
        self._update_count()

    def sources(self, recursive: bool) -> list[Path]:
        return collect_sources(self.paths(), recursive)

    def _remove_selected(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self._update_count()

    def _update_count(self) -> None:
        self.count.setText(f"Элементов: {self.list.count()}")

    def _pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Выберите видео", "", VIDEO_FILTER)
        self.add_paths(paths)

    def _pick_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Выберите папку с видео")
        if path:
            self.add_paths([path])

    # ------------------------------------------------------------ preset I/O

    def load(self, preset: Preset) -> None:
        self.output.load(preset)

    def apply(self, preset: Preset) -> Preset:
        return self.output.apply(preset)
