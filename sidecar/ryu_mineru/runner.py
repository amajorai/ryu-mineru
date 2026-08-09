"""Subprocess supervision — the one place this backend differs from its siblings.

`unstructured` parses in-process, so its watchdog can only *stop waiting* on a
runaway parse: CPython cannot kill a thread. MinerU is a CLI, so the same
laissez-faire would leave a multi-gigabyte model-loading process alive after the
job it belonged to was marked `failed`. On a 16 GB machine, two orphans are the
whole machine.

So the timeout here is real:

* the child is started in its **own session** (`start_new_session=True`), which
  makes it a process-group leader. MinerU spawns torch dataloader workers and,
  on the VLM backends, an inference server child; `proc.kill()` would reap the
  CLI wrapper and orphan those. We signal the *group*.
* SIGTERM first (MinerU exits cleanly and releases GPU memory), then SIGKILL
  after `KILL_GRACE_SECS`, then `wait()` — always reaped, never a zombie.
* `DELETE /jobs/{job_id}` routes into the same kill path. A cooperative cancel
  that leaves a ten-minute GPU parse running is a cancel in name only.

The other quiet hazard is output plumbing. MinerU is chatty (tqdm progress bars,
model-download logs), and `Popen(stdout=PIPE)` + `wait()` deadlocks the moment the
64 KiB pipe buffer fills — the child blocks on write, the parent blocks on wait,
and the timeout then "kills a hung parse" that was only ever stuck talking to us.
Both streams therefore go to a file we own and read back afterwards. This is the
failure mode that passes a small-fixture smoke test and hangs on a real 300-page
PDF.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Sequence

from .limits import KILL_GRACE_SECS

_POSIX = os.name == "posix"

# How much of the child's log we keep for an error message. Enough to carry a
# traceback's final frames; not so much that a failed job's `error` string
# becomes a megabyte of tqdm redraws.
LOG_TAIL_BYTES = 4000


class Cancelled(RuntimeError):
    """The job was cancelled while this step was running."""


class StepTimeout(RuntimeError):
    """The job's wall-clock budget ran out. The child has already been killed."""


def _process_table() -> list[tuple[int, int, int]]:
    """`(pid, ppid, pgid)` for every visible process, or empty if ps(1) fails."""
    try:
        completed = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,pgid="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    rows: list[tuple[int, int, int]] = []
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), int(parts[2])))
        except ValueError:
            continue
    return rows


def _descendants(root_pid: int) -> list[tuple[int, int]]:
    """`(pid, pgid)` for everything under `root_pid`. Best effort, never raises."""
    rows = _process_table()
    by_parent: dict[int, list[tuple[int, int]]] = {}
    for pid, ppid, pgid in rows:
        by_parent.setdefault(ppid, []).append((pid, pgid))
    seen: set[int] = set()
    found: list[tuple[int, int]] = []
    stack = [root_pid]
    while stack:
        for pid, pgid in by_parent.get(stack.pop(), ()):
            if pid in seen or pid == root_pid:
                continue
            seen.add(pid)
            found.append((pid, pgid))
            stack.append(pid)
    return found


def _sweep_escaped_groups(recorded: list[tuple[int, int]], own_pgid: Optional[int]) -> None:
    """Kill descendants that put themselves in a *different* process group.

    MinerU's CLI really does this: it starts a temporary local `mineru-api`
    FastAPI service as its own group leader, so `killpg` on our group does not
    reach it. In practice SIGTERM to the CLI shuts that service down politely —
    but "in practice" is not the same as "always", and a survivor here is a
    long-lived HTTP server holding model memory on a 16 GB machine.

    Pid-reuse safety: a recorded pid is only signalled if it is *still* present
    with the *same* process-group id. A pid recycled within the grace window that
    also lands in the same pgid is not a plausible accident.
    """
    escaped = {pgid for _pid, pgid in recorded if pgid not in (own_pgid, 0, 1)}
    if not escaped:
        return
    live = {(pid, pgid) for pid, _ppid, pgid in _process_table()}
    for pgid in escaped:
        if not any((pid, pgid) in live for pid, recorded_pgid in recorded if recorded_pgid == pgid):
            continue
        for sig in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                break


def _kill_tree(proc: subprocess.Popen, grace: float = float(KILL_GRACE_SECS)) -> None:
    """Terminate `proc` and everything it spawned, then reap it.

    Signalling the process group is the point: MinerU's children (the temporary
    local API service, torch dataloader workers, the inference engine on the VLM
    backends) do not die with their parent, and `proc.kill()` would reap the CLI
    wrapper while leaving multiple gigabytes of model state resident. Falls back
    to the process itself where process groups do not exist.
    """
    if proc.poll() is not None:
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        return

    pgid: Optional[int] = None
    escapees: list[tuple[int, int]] = []
    if _POSIX:
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = None
        # Snapshot the tree *before* signalling: once the parents die, the
        # survivors are reparented to init and their ancestry is unrecoverable.
        escapees = _descendants(proc.pid)

    # SIGTERM first — MinerU exits cleanly, releases GPU memory and shuts down the
    # temporary API service it started. SIGKILL only once politeness has failed.
    # If both are ignored (uninterruptible sleep on a dead mount, typically) there
    # is nothing more to do from here: the job is already terminal and the OS
    # reaps the child when it unblocks.
    for sig, patience in (
        (signal.SIGTERM, grace),
        (getattr(signal, "SIGKILL", signal.SIGTERM), grace),
    ):
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            elif sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=patience)
            break
        except subprocess.TimeoutExpired:
            continue

    if _POSIX and escapees:
        _sweep_escaped_groups(escapees, pgid)


def _tail(path: Path, limit: int = LOG_TAIL_BYTES) -> str:
    """Last `limit` bytes of a child's combined output, as text."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(size - limit)
            raw = handle.read()
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace").strip()


class ProcessRunner:
    """Runs at most one child at a time under a single job-wide deadline.

    One runner per job, not per step: an archive of 512 members must share the
    job's 600 s budget, not be handed 512 × 600 s. `remaining()` is what each
    step gets.
    """

    def __init__(self, deadline: float) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._cancelled = False
        self.deadline = deadline

    def remaining(self) -> float:
        return self.deadline - time.time()

    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def checkpoint(self) -> None:
        """Raise if the job is over budget or cancelled.

        Called between steps so that work done *outside* a child process —
        archive expansion, reading a large markdown tree back — is bounded too.
        """
        if self.cancelled():
            raise Cancelled("job cancelled")
        if self.remaining() <= 0:
            raise StepTimeout("job deadline exceeded")

    def cancel(self) -> None:
        """Mark cancelled and kill whatever child is live right now."""
        with self._lock:
            self._cancelled = True
            proc = self._proc
        if proc is not None:
            _kill_tree(proc)

    def run(self, argv: Sequence[str], *, cwd: Path, env: dict[str, str], log_path: Path) -> str:
        """Run one child to completion, returning the tail of its output.

        Raises `StepTimeout` (child killed), `Cancelled` (child killed), or
        `subprocess.CalledProcessError`-shaped failure via a non-zero return
        code, which the caller turns into a typed `ParseError`.
        """
        self.checkpoint()
        budget = self.remaining()

        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("wb") as log:
            # argv is a list and `shell` is never set: the input path is
            # attacker-influenced and must not be re-parsed by a shell.
            popen_kwargs: dict[str, object] = {
                "cwd": str(cwd),
                "env": env,
                "stdin": subprocess.DEVNULL,
                "stdout": log,
                "stderr": subprocess.STDOUT,
            }
            if _POSIX:
                # Own session => own process group => killpg reaches the whole
                # tree. Also detaches the child from our controlling terminal so
                # a Ctrl-C on a dev run does not race us to the kill.
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(list(argv), **popen_kwargs)  # type: ignore[arg-type]

            with self._lock:
                if self._cancelled:
                    # Cancelled between checkpoint and spawn: kill it now, since
                    # `cancel()` did not see this child.
                    self._proc = None
                    _kill_tree(proc)
                    raise Cancelled("job cancelled")
                self._proc = proc

            try:
                code = proc.wait(timeout=budget)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                raise StepTimeout("job deadline exceeded") from None
            finally:
                with self._lock:
                    self._proc = None

        if self.cancelled():
            raise Cancelled("job cancelled")
        output = _tail(log_path)
        if code != 0:
            raise ChildFailed(code, output)
        return output


class ChildFailed(RuntimeError):
    """A child exited non-zero. Carries the code and the tail of its output."""

    def __init__(self, code: int, output: str) -> None:
        super().__init__(f"exited with code {code}")
        self.code = code
        self.output = output
