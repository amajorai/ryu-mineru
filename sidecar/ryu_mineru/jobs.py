"""In-process parse-job table.

Jobs live only for the life of the process — Core owns the durable record. The
table is bounded (`MAX_JOBS`) because a finished job holds a whole document's
markdown and this sidecar is meant to idle-stop, not to grow.

**Timeout enforcement here is stricter than in the `unstructured` backend, and
deliberately so.** That one parses in-process, where CPython cannot kill a
thread, so its watchdog can only stop waiting and let the work run on. MinerU is
a subprocess: the deadline is handed to `ProcessRunner`, which waits on the child
with a timeout and then SIGTERM/SIGKILLs its whole process group. A timed-out or
cancelled job leaves nothing behind — no orphaned CLI, no orphaned dataloader
workers, no model still pinned in GPU memory. On a machine that meets MinerU's
16 GB floor with nothing to spare, an orphan is not a leak, it is an outage.

The budget covers the whole job, not one child, and it starts when a worker slot
is acquired rather than at submit — queueing behind a busy node must not eat a
document's parse time.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from .limits import MAX_JOBS, MAX_WORKERS, TIMEOUT_SECS
from .parser import ParseError, parse_file
from .runner import Cancelled, ProcessRunner

JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
TERMINAL: set[str] = {"succeeded", "failed", "cancelled"}


@dataclass
class Job:
    id: str
    filename: str
    status: JobStatus = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    missing_dependencies: list[str] = field(default_factory=list)
    result: Optional[dict[str, Any]] = None
    _cancel: threading.Event = field(default_factory=threading.Event)
    # The live subprocess supervisor, present only while the job is running. This
    # is what makes DELETE /jobs/{id} a real cancel rather than a status change.
    _runner: Optional[ProcessRunner] = None

    def snapshot(self, *, include_result: bool = True) -> dict[str, Any]:
        """The poll payload. `result` is null until the job succeeds."""
        return {
            "job_id": self.id,
            "status": self.status,
            "filename": self.filename,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "error_code": self.error_code,
            "missing_dependencies": self.missing_dependencies,
            "result": self.result if include_result else None,
        }


class JobStore:
    """Thread-safe job registry with a bounded worker pool."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._slots = threading.Semaphore(MAX_WORKERS)

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return [self._jobs[job_id] for job_id in self._order if job_id in self._jobs]

    def cancel(self, job_id: str) -> Optional[Job]:
        """Cooperative cancel that actually stops the work.

        A queued job never starts; a running one has its MinerU process group
        killed before the call returns, so a caller that gave up does not leave a
        ten-minute GPU parse burning the node.
        """
        job = self.get(job_id)
        if job is None or job.status in TERMINAL:
            return job
        job._cancel.set()
        runner = job._runner
        if runner is not None:
            runner.cancel()
        job.status = "cancelled"
        job.finished_at = time.time()
        return job

    def submit(self, path: Path, options: dict[str, Any]) -> Job:
        job = Job(id=f"parse_{uuid.uuid4().hex[:16]}", filename=path.name)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._evict_locked()
        threading.Thread(
            target=self._run, args=(job, path, options), name=f"parse-{job.id}", daemon=True
        ).start()
        return job

    def _evict_locked(self) -> None:
        """Drop the oldest terminal jobs once the table is over budget."""
        while len(self._order) > MAX_JOBS:
            for index, job_id in enumerate(self._order):
                if self._jobs.get(job_id) and self._jobs[job_id].status in TERMINAL:
                    self._order.pop(index)
                    self._jobs.pop(job_id, None)
                    break
            else:
                # Nothing terminal to reclaim — every tracked job is still live.
                return

    def _run(self, job: Job, path: Path, options: dict[str, Any]) -> None:
        # Bound concurrency here rather than at submit so the HTTP handler always
        # answers 202 immediately; queued work waits on this semaphore, not the
        # caller's socket.
        self._slots.acquire()
        runner: Optional[ProcessRunner] = None
        try:
            if job._cancel.is_set():
                return
            job.status = "running"
            job.started_at = time.time()
            runner = ProcessRunner(job.started_at + TIMEOUT_SECS)
            job._runner = runner
            if job._cancel.is_set():
                # Cancelled in the window before the runner was attached; nothing
                # has spawned yet, but be explicit rather than racy.
                runner.cancel()
                return
            try:
                result = parse_file(path, options, runner)
            except Cancelled:
                # `cancel()` already set the terminal state and killed the child.
                return
            except ParseError as exc:
                self._fail(job, exc.code, str(exc), missing=exc.missing)
                return
            except Exception as exc:  # noqa: BLE001 — a crash must not lose the job
                self._fail(job, "parse_failed", f"{type(exc).__name__}: {exc}")
                return
            if job._cancel.is_set():
                return
            job.result = result
            job.status = "succeeded"
            job.finished_at = time.time()
        finally:
            job._runner = None
            self._slots.release()

    @staticmethod
    def _fail(job: Job, code: str, message: str, *, missing: list[str] | None = None) -> None:
        job.status = "failed"
        job.error_code = code
        job.error = message
        job.missing_dependencies = missing or []
        job.finished_at = time.time()


STORE = JobStore()
