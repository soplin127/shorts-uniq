"""Queue tab: batch settings, per-job table, overall progress, controls and log."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.batch import BatchCallbacks, BatchRunner, JobResult, format_eta
from core.config import Preset
from core.ffmpeg import FFmpegTools
from gui.forms import SectionForm, batch_groups

JobRequest = tuple[FFmpegTools, Preset, list[Path], Path]
STATUS_LABELS = {
    "queued": "В очереди",
    "running": "Обработка",
    "done": "Готово",
    "skipped": "Пропущено",
    "failed": "Ошибка",
    "stopped": "Остановлено",
}
PROGRESS_STEPS = 1000
LOG_MAX_LINES = 5000
STOP_WAIT_SECONDS = 15.0
COL_FILE, COL_VARIANT, COL_STATUS, COL_PROGRESS = range(4)


class BatchSignals(QObject):
    """Carries BatchRunner callbacks from worker threads to the GUI thread."""

    job_status = Signal(int, str, str)
    job_progress = Signal(int, float)
    overall = Signal(int, int, object)
    log = Signal(str)
    finished = Signal(object)

    def callbacks(self) -> BatchCallbacks:
        return BatchCallbacks(
            on_job_status=lambda job, status, msg: self.job_status.emit(job.index, status, msg),
            on_job_progress=lambda job, frac: self.job_progress.emit(job.index, frac),
            on_overall=lambda done, total, eta: self.overall.emit(done, total, eta),
            on_log=self.log.emit,
        )


class QueueTab(QWidget):
    batch_finished = Signal(object)  # list[JobResult]

    def __init__(self, request_job: Callable[[bool], JobRequest | None],
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.request_job = request_job
        self.runner: BatchRunner | None = None
        self._thread: threading.Thread | None = None
        self._preview = False
        self._out_dir: Path | None = None

        self.form = SectionForm(batch_groups())
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Файл", "Вариант", "Статус", "Прогресс"])
        self.table.horizontalHeader().setSectionResizeMode(COL_FILE, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.overall = QProgressBar()
        self.eta = QLabel("—")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(LOG_MAX_LINES)

        self.buttons = {}
        controls = QHBoxLayout()
        for key, text, slot in (("start", "Старт", lambda: self.start(preview=False)),
                                ("preview", "Превью", lambda: self.start(preview=True)),
                                ("pause", "Пауза", self.pause),
                                ("resume", "Продолжить", self.resume),
                                ("stop", "Стоп", self.stop),
                                ("open", "Открыть папку", self.open_output)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            controls.addWidget(button)
            self.buttons[key] = button

        progress = QHBoxLayout()
        progress.addWidget(self.overall, 1)
        progress.addWidget(self.eta)
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(self.table)
        top_layout.addLayout(progress)
        top_layout.addLayout(controls)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.form)
        splitter.addWidget(top)
        splitter.addWidget(self.log)
        splitter.setStretchFactor(1, 2)
        QVBoxLayout(self).addWidget(splitter)

        self.signals = BatchSignals()
        self.signals.job_status.connect(self._on_status)
        self.signals.job_progress.connect(self._on_progress)
        self.signals.overall.connect(self._on_overall)
        self.signals.log.connect(self.log.appendPlainText)
        self.signals.finished.connect(self._on_finished)
        self._update_buttons()

    # ------------------------------------------------------------ preset I/O

    def load(self, preset: Preset) -> None:
        self.form.load(preset)

    def apply(self, preset: Preset) -> Preset:
        return self.form.apply(preset)

    # --------------------------------------------------------------- control

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, preview: bool) -> None:
        if self.running:
            return
        request = self.request_job(preview)
        if request is None:
            return
        tools, preset, sources, out_dir = request
        self._preview = preview
        self._out_dir = out_dir
        self.runner = BatchRunner(tools, preset, sources, out_dir, self.signals.callbacks(),
                                  preview=preview)
        self._fill_table()
        self.log.clear()
        self._thread = threading.Thread(target=self._run, args=(self.runner,),
                                        name="uniq-batch", daemon=True)
        self._thread.start()
        self._update_buttons()

    def _run(self, runner: BatchRunner) -> None:
        results: list[JobResult] = []
        try:
            results = runner.run()
        except Exception as exc:  # noqa: BLE001 - report anything to the GUI instead of dying silently
            self.signals.log.emit(f"ОШИБКА запуска: {exc}")
        finally:
            self.signals.finished.emit(results)

    def pause(self) -> None:
        if self.running and not self.runner.paused:
            self.runner.pause()
            self._update_buttons()

    def resume(self) -> None:
        if self.running and self.runner.paused:
            self.runner.resume()
            self._update_buttons()

    def stop(self) -> None:
        if self.running:
            self.runner.stop()
            self._update_buttons()

    def stop_and_wait(self, timeout: float = STOP_WAIT_SECONDS) -> None:
        if self.running:
            self.runner.stop()
            self._thread.join(timeout)

    def open_output(self) -> None:
        if self._out_dir and self._out_dir.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._out_dir)))

    def _update_buttons(self) -> None:
        running = self.running
        paused = running and self.runner.paused
        self.buttons["start"].setEnabled(not running)
        self.buttons["preview"].setEnabled(not running)
        self.buttons["pause"].setEnabled(running and not paused)
        self.buttons["resume"].setEnabled(paused)
        self.buttons["stop"].setEnabled(running)
        self.buttons["open"].setEnabled(self._out_dir is not None)

    # ----------------------------------------------------------------- table

    def _fill_table(self) -> None:
        jobs = self.runner.jobs
        self.table.setRowCount(len(jobs))
        for row, job in enumerate(jobs):
            self.table.setItem(row, COL_FILE, QTableWidgetItem(job.source.name))
            self.table.setItem(row, COL_VARIANT, QTableWidgetItem(str(job.variant)))
            self.table.setItem(row, COL_STATUS, QTableWidgetItem(STATUS_LABELS["queued"]))
            bar = QProgressBar()
            bar.setRange(0, PROGRESS_STEPS)
            self.table.setCellWidget(row, COL_PROGRESS, bar)
        self.overall.setRange(0, max(len(jobs), 1))
        self.overall.setValue(0)
        self.eta.setText("—")

    def status_text(self, row: int) -> str:
        return self.table.item(row, COL_STATUS).text()

    def overall_value(self) -> tuple[int, int]:
        return self.overall.value(), self.overall.maximum()

    def _on_status(self, index: int, status: str, message: str) -> None:
        item = self.table.item(index, COL_STATUS)
        item.setText(STATUS_LABELS.get(status, status))
        item.setToolTip(message)
        if status in ("done", "skipped"):
            self._on_progress(index, 1.0)

    def _on_progress(self, index: int, frac: float) -> None:
        bar = self.table.cellWidget(index, COL_PROGRESS)
        bar.setValue(round(min(max(frac, 0.0), 1.0) * PROGRESS_STEPS))

    def _on_overall(self, done: int, total: int, eta) -> None:
        self.overall.setRange(0, max(total, 1))
        self.overall.setValue(done)
        self.eta.setText(f"{done} / {total} · осталось {format_eta(eta)}")

    def _on_finished(self, results: list[JobResult]) -> None:
        if self._thread is not None:
            self._thread.join()
        self._update_buttons()
        if self._preview:
            output = next((r.output for r in results if r.status == "done" and r.output), None)
            if output:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(output)))
        self.batch_finished.emit(results)
