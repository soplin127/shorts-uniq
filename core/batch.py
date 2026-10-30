"""Parallel job queue with pause/resume/stop, resume-after-restart, log and CSV report."""
from __future__ import annotations

import os
import random
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from core.config import Preset
from core.encoders import SOFTWARE, resolve_encoder
from core.ffmpeg import (
    FFmpegError,
    FFmpegProcess,
    FFmpegTools,
    capabilities,
    probe,
    run_ffmpeg,
)
from core.naming import pick_output_path, render_name
from core.pipeline import JobContext, JobPlan, plan_job
from core.report import CsvReport, ReportRow, params_to_json
from core.state import StateStore
from features.metadata import roll_metadata, verify_output

LOG_FILE = "uniq.log"
MIN_PROGRESS_FOR_ETA = 0.02


class JobStopped(Exception):
    pass


@dataclass(frozen=True)
class Job:
    index: int
    source: Path
    variant: int

    @property
    def label(self) -> str:
        return f"{self.source.name} #{self.variant}"


@dataclass
class BatchCallbacks:
    on_job_status: Callable[[Job, str, str], None] = lambda job, status, msg: None
    on_job_progress: Callable[[Job, float], None] = lambda job, frac: None
    on_overall: Callable[[int, int, float | None], None] = lambda done, total, eta: None
    on_log: Callable[[str], None] = lambda line: None


@dataclass(frozen=True)
class JobResult:
    job: Job
    status: str  # done | skipped | failed | stopped
    output: Path | None = None
    error: str = ""


def default_workers() -> int:
    return max(1, (os.cpu_count() or 2) // 2)


def job_rng(seed: str, job: Job) -> random.Random:
    if not seed:
        return random.Random()
    return random.Random(f"{seed}:{job.source.name}:{job.variant}")


class BatchRunner:
    def __init__(
        self,
        tools: FFmpegTools,
        preset: Preset,
        sources: list[Path],
        out_dir: Path,
        callbacks: BatchCallbacks | None = None,
        *,
        preview: bool = False,
    ):
        self.tools = tools
        self.preset = preset
        self.out_dir = out_dir
        self.cb = callbacks or BatchCallbacks()
        self.preview = preview
        variants = 1 if preview else preset.output.variants
        self.jobs = [
            Job(i, src, v)
            for i, (src, v) in enumerate((s, v) for s in sources for v in range(1, variants + 1))
        ]
        self._running = threading.Event()
        self._running.set()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._processes: dict[int, FFmpegProcess] = {}
        self._progress: dict[int, float] = {}
        self._finished = 0
        self._taken_names: set[str] = set()
        self._started_at = 0.0
        self.results: list[JobResult] = []
        self.report: CsvReport | None = None
        self.encoder = SOFTWARE

    # ---------------------------------------------------------------- control

    def pause(self) -> None:
        self._running.clear()
        with self._lock:
            for proc in self._processes.values():
                proc.suspend()
        self._log("Пауза")

    def resume(self) -> None:
        with self._lock:
            for proc in self._processes.values():
                proc.resume()
        self._running.set()
        self._log("Продолжение")

    def stop(self) -> None:
        self._stop.set()
        self._running.set()  # wake paused workers so they can exit
        with self._lock:
            for proc in self._processes.values():
                proc.terminate()
        self._log("Остановка")

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    # -------------------------------------------------------------------- run

    def run(self) -> list[JobResult]:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        caps = capabilities(self.tools.ffmpeg)
        self.encoder, warning = resolve_encoder(
            self.tools.ffmpeg, self.preset.encoder.hwaccel, caps.encoders)
        if warning:
            self._log(warning)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.report = CsvReport(self.out_dir / f"report_{stamp}.csv")
        state = StateStore(self.out_dir)
        workers = 1 if self.preview else (self.preset.batch.workers or default_workers())
        self._log(f"Старт: {len(self.jobs)} задач, потоков: {workers}, энкодер: {self.encoder}")
        self._started_at = time.monotonic()
        self._emit_overall()
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="uniq") as pool:
            futures = [pool.submit(self._run_job, job, caps, state) for job in self.jobs]
            results = [f.result() for f in futures]
        self.results = results
        failed = sum(r.status == "failed" for r in results)
        self._log(f"Готово: {sum(r.status == 'done' for r in results)}, ошибок: {failed}")
        return results

    def _run_job(self, job: Job, caps, state: StateStore) -> JobResult:
        try:
            result = self._process(job, caps, state)
        except JobStopped:
            result = JobResult(job, "stopped")
        except Exception as exc:  # noqa: BLE001 - one bad file must not kill the batch
            result = JobResult(job, "failed", error=str(exc))
            self._log(f"ОШИБКА {job.label}: {exc}")
            self.report.add(ReportRow(source=str(job.source), variant=job.variant,
                                      status="failed", error=str(exc)[:500]))
        self.cb.on_job_status(job, result.status, result.error)
        with self._lock:
            self._finished += 1
            self._progress.pop(job.index, None)
        self._emit_overall()
        return result

    def _process(self, job: Job, caps, state: StateStore) -> JobResult:
        self._running.wait()
        if self._stop.is_set():
            raise JobStopped
        if self.preset.batch.skip_existing and not self.preview:
            existing = state.finished_output(job.source, job.variant)
            if existing:
                self._log(f"Пропуск {job.label}: уже есть {existing.name}")
                return JobResult(job, "skipped", existing)

        self.cb.on_job_status(job, "running", "")
        started = time.monotonic()
        info = probe(self.tools, job.source)
        if not info.has_video or info.duration <= 0:
            raise FFmpegError("в файле нет видеопотока или длительность неизвестна")

        rng = job_rng(self.preset.batch.seed, job)
        metadata = roll_metadata(self.preset.metadata, rng)
        final = self._reserve_name(job, rng, metadata)
        part = final.with_name(f".{final.stem}.part.mp4")
        work_dir = Path(tempfile.mkdtemp(prefix="uniq_"))
        try:
            plan, encoder = self._encode(job, info, caps, metadata, part, work_dir)
            if self.preset.metadata.verify:
                problems = verify_output(self.tools, str(part), metadata, info.tags)
                if problems:
                    raise FFmpegError("проверка результата: " + "; ".join(problems))
            os.replace(part, final)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
            part.unlink(missing_ok=True)

        if not self.preview:
            state.mark_done(job.source, job.variant, final)
        elapsed = time.monotonic() - started
        self._write_report(job, final, plan, encoder, elapsed)
        self._log(f"OK {job.label} -> {final.name} ({elapsed:.1f} c)")
        return JobResult(job, "done", final)

    def _encode(self, job, info, caps, metadata, part, work_dir) -> tuple[JobPlan, str]:
        """Encode once; if a hardware encoder fails mid-way, retry the job with libx264."""
        encoders = [self.encoder] if self.encoder == SOFTWARE else [self.encoder, SOFTWARE]
        last_error: Exception | None = None
        for encoder in encoders:
            ctx = JobContext(self.tools, caps, self.preset, encoder, work_dir)
            # a fresh rng per attempt keeps seeded runs identical across the retry
            rng = job_rng(f"{self.preset.batch.seed}:plan" if self.preset.batch.seed else "", job)
            preview = self.preset.batch.preview_seconds if self.preview else None
            plan = plan_job(ctx, info, rng, part, metadata, preview_seconds=preview)
            try:
                run_ffmpeg(
                    plan.command,
                    cwd=str(work_dir),
                    stderr_path=work_dir / "ffmpeg.log",
                    expected_duration=plan.expected_duration,
                    on_progress=lambda frac: self._job_progress(job, frac),
                    on_start=lambda proc: self._register(job, proc),
                )
                return plan, encoder
            except FFmpegError as exc:
                if self._stop.is_set():
                    raise JobStopped from exc
                last_error = exc
                if encoder != SOFTWARE:
                    self._log(f"{job.label}: {encoder} не справился, повтор на libx264")
            finally:
                with self._lock:
                    self._processes.pop(job.index, None)
        raise last_error  # type: ignore[misc]

    # ---------------------------------------------------------------- helpers

    def _reserve_name(self, job: Job, rng: random.Random, metadata) -> Path:
        template = self.preset.output.name_template
        if self.preview:
            template = f"preview_{template}"

        def make_stem() -> str:
            return render_name(template, rng, date_compact=metadata.date_compact,
                               time_compact=metadata.time_compact, stem=job.source.stem,
                               variant=job.variant, index=job.index + 1)

        with self._lock:
            return pick_output_path(self.out_dir, make_stem, self._taken_names)

    def _register(self, job: Job, proc: FFmpegProcess) -> None:
        with self._lock:
            self._processes[job.index] = proc
            if self._stop.is_set():
                proc.terminate()
            elif self.paused:
                proc.suspend()

    def _job_progress(self, job: Job, frac: float) -> None:
        with self._lock:
            self._progress[job.index] = frac
        self.cb.on_job_progress(job, frac)
        self._emit_overall()

    def _emit_overall(self) -> None:
        with self._lock:
            total = len(self.jobs)
            done = self._finished
            fraction = (done + sum(self._progress.values())) / total if total else 1.0
        elapsed = time.monotonic() - self._started_at
        eta = None
        if MIN_PROGRESS_FOR_ETA <= fraction < 1.0:
            eta = elapsed / fraction * (1.0 - fraction)
        self.cb.on_overall(done, total, eta)

    def _write_report(self, job: Job, output: Path, plan: JobPlan, encoder: str, elapsed: float):
        self.report.add(ReportRow(
            source=str(job.source), output=output.name, variant=job.variant, status="done",
            elapsed_s=round(elapsed, 2), encoder=encoder, crf=plan.crf,
            audio_kbps=plan.audio_bitrate_k,
            music=plan.notes.get("music", ""), subtitles=plan.notes.get("subtitles", ""),
            ads=plan.notes.get("ads", ""), params_json=params_to_json(plan.uniq),
        ))

    def _log(self, message: str) -> None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
        try:
            with self._lock, open(self.out_dir / LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass  # the log file is a convenience; callbacks still get the line
        self.cb.on_log(line)


def with_overrides(preset: Preset, **sections) -> Preset:
    """Return a copy of ``preset`` with whole sections replaced (CLI/GUI helper)."""
    return replace(preset, **sections)
