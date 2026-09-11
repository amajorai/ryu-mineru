"""Locating and invoking the `mineru` command line.

MinerU ships as a CLI, not as a library with a stable public API, so this sidecar
shells out. Two consequences shape this module:

* **Resolution beats `which`.** Core installs the sidecar into a per-app venv, and
  a venv's console scripts live in `sys.executable`'s own `bin/` — which is not on
  `PATH` for a process that was started by absolute path. So we look beside the
  running interpreter first, then fall back to `PATH`, then to an explicit
  `RYU_MINERU_CLI` override for hosts that installed MinerU somewhere else.
* **Nothing here imports MinerU.** `import mineru` drags in torch and the model
  registry; doing that at `/health` time would make a liveness probe cost several
  seconds and hundreds of megabytes. The version comes from package metadata and
  the capability check is "does the executable exist".

Argument policy: the base invocation is exactly the three flags MinerU documents
(`-p`, `-o`, `-b`). Every other flag is passed **only when explicitly requested**
through `options` or env, so a flag name that shifts between MinerU releases can
only break the users who asked for it, never the default path. Values are matched
against closed sets or tight patterns — an `options` dict arrives from a caller
and must not be able to grow the command line.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

# MinerU's parse backends. The union of two generations on purpose: 2.x named the
# accelerated ones after their inference stack (`vlm-sglang-engine`, later
# `vlm-vllm-engine`), 3.x collapsed those into `vlm-engine` and added `hybrid-*`.
# Which of these the installed release accepts is the installed release's
# business — an unknown name makes the CLI exit non-zero with its own list, which
# reaches the caller as a `parse_failed` naming the valid values. What this tuple
# is for is keeping an arbitrary caller string off the command line.
BACKENDS = (
    "pipeline",
    # 3.x
    "vlm-engine",
    "vlm-http-client",
    "hybrid-engine",
    "hybrid-http-client",
    # 2.x
    "vlm-transformers",
    "vlm-sglang-engine",
    "vlm-sglang-client",
    "vlm-vllm-engine",
    "vlm-vllm-async-engine",
)

# `pipeline` is the only backend that runs on a CPU-only host, and — critically —
# it is NOT MinerU's own default: 3.x defaults to `hybrid-engine`, which wants a
# GPU. Inheriting upstream's default would make every parse on a laptop either
# fail or crawl, so this sidecar always passes `-b` explicitly.
DEFAULT_BACKEND = "pipeline"

# Pipeline/hybrid content strategy: auto-detect, treat as born-digital text, or
# force OCR.
METHODS = ("auto", "txt", "ocr")

# Hybrid-backend quality/latency dial (3.x only).
EFFORTS = ("medium", "high")

_LANG_RE = re.compile(r"^[a-z][a-z_]{1,19}$")
_DEVICE_RE = re.compile(r"^(cpu|cuda|npu|mps|xpu)(:\d{1,2})?$")


def cli_path() -> Optional[Path]:
    """Absolute path to the `mineru` executable, or None if it is not installed."""
    override = (os.environ.get("RYU_MINERU_CLI") or "").strip()
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None

    # The venv Core built for this sidecar. Its console scripts sit next to the
    # interpreter, and that install is the one we provisioned — preferred over
    # whatever an unrelated `PATH` entry might offer.
    #
    # Deliberately NOT `Path(sys.executable).resolve()`: in a venv built by `uv`
    # (and by `python -m venv --symlinks`, the default on POSIX) `bin/python` is a
    # symlink to the base interpreter, so resolving it walks straight out of the
    # venv and into a directory that has no `mineru` in it. That failure is
    # silent — the sidecar reports `available: false` on a host where MinerU is
    # installed and working.
    scripts_dir = "Scripts" if os.name == "nt" else "bin"
    bindirs = [Path(sys.executable).parent, Path(sys.prefix) / scripts_dir]
    for bindir in bindirs:
        for name in ("mineru", "mineru.exe"):
            candidate = bindir / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate

    found = shutil.which("mineru")
    return Path(found) if found else None


def mineru_version() -> Optional[str]:
    """Installed MinerU version from package metadata, or None.

    Read from metadata rather than by running `mineru --version`: the CLI imports
    torch on startup, and /health must stay cheap.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover — 3.8+
        return None
    for name in ("mineru", "magic-pdf"):
        try:
            return version(name)
        except PackageNotFoundError:
            continue
        except Exception:
            return None
    return None


def mineru_major() -> Optional[int]:
    """Major version of the installed MinerU, or None.

    Load-bearing for exactly one thing: 3.x reads OOXML Office documents itself,
    while 2.x needed a LibreOffice conversion first. Offering `.docx` on a host
    that cannot actually read it is the "plausible-looking lie" this capability
    exists to kill, so the format list asks this question.
    """
    version = mineru_version()
    if not version:
        return None
    try:
        return int(version.split(".", 1)[0])
    except ValueError:
        return None


def configured_backend() -> str:
    """The parse backend this node defaults to.

    `pipeline` unless the operator says otherwise: it is the only one that runs
    on a CPU-only host, and a backend that needs 8 GB of VRAM is a terrible
    default for a node that may not have a GPU at all.
    """
    raw = (os.environ.get("RYU_MINERU_BACKEND") or "").strip().lower()
    return raw if raw in BACKENDS else DEFAULT_BACKEND


def _clean_str(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def resolve_backend(options: dict[str, Any]) -> str:
    """The backend one parse will actually use: request, else node default.

    A request naming a backend we do not recognise falls back rather than
    failing — unknown option *values* follow the same "ignored, never an error"
    rule as unknown option keys.
    """
    requested = _clean_str(options.get("backend"))
    if requested and requested.lower() in BACKENDS:
        return requested.lower()
    return configured_backend()


def build_argv(cli: Path, source: Path, outdir: Path, options: dict[str, Any]) -> list[str]:
    """The concrete command line for one parse.

    Unknown option keys are ignored, never an error — a hint one `document.parse`
    backend understands must not fail a parse on another.
    """
    backend = resolve_backend(options)

    argv = [str(cli), "-p", str(source), "-o", str(outdir), "-b", backend]

    # --- everything below is opt-in ------------------------------------------
    method = _clean_str(options.get("method"))
    if method and method.lower() in METHODS and not backend.startswith("vlm"):
        argv += ["-m", method.lower()]

    effort = _clean_str(options.get("effort"))
    if effort and effort.lower() in EFFORTS and backend.startswith("hybrid"):
        argv += ["--effort", effort.lower()]

    lang = _clean_str(options.get("lang")) or _clean_str(os.environ.get("RYU_MINERU_LANG"))
    if lang and _LANG_RE.match(lang.lower()):
        argv += ["-l", lang.lower()]

    # NOTE: there is deliberately no `-d/--device` here. MinerU takes the device
    # from the `MINERU_DEVICE_MODE` environment variable, not from a flag — see
    # `child_env`. Passing a flag it does not define would be silently swallowed
    # (its CLI sets `ignore_unknown_options`), which is the worst outcome: the
    # user asks for CPU, nothing errors, and the parse still tries the GPU.

    if options.get("formula") is not None:
        argv += ["-f", "true" if options["formula"] else "false"]
    if options.get("table") is not None:
        argv += ["-t", "true" if options["table"] else "false"]

    start = options.get("start_page")
    if isinstance(start, int) and not isinstance(start, bool) and start >= 0:
        argv += ["-s", str(start)]
    end = options.get("end_page")
    if isinstance(end, int) and not isinstance(end, bool) and end >= 0:
        argv += ["-e", str(end)]

    return argv


def child_env(workdir: Path, options: dict[str, Any] | None = None) -> dict[str, str]:
    """Environment for the MinerU child.

    Inherited, with the model/cache homes pinned under a directory we own so a
    parse cannot scatter multi-gigabyte model downloads across `$HOME`, and with
    the bearer token stripped: the child has no business holding Core's shared
    secret, and MinerU never calls back into Ryu.

    Device selection lives here rather than on the command line because that is
    where MinerU reads it (`utils/config_reader.py` → `MINERU_DEVICE_MODE`).
    """
    env = dict(os.environ)
    env.pop("RYU_EXT_TOKEN", None)
    cache = workdir / "cache"
    env.setdefault("HF_HOME", str(cache / "hf"))
    env.setdefault("TORCH_HOME", str(cache / "torch"))
    env.setdefault("MINERU_MODEL_SOURCE", "huggingface")
    # tqdm's carriage-return redraws are worthless in a log file and inflate the
    # tail we keep for error messages.
    env.setdefault("TQDM_DISABLE", "1")

    device = _clean_str((options or {}).get("device")) or _clean_str(
        os.environ.get("RYU_MINERU_DEVICE")
    )
    if device and _DEVICE_RE.match(device.lower()):
        env["MINERU_DEVICE_MODE"] = device.lower()
    return env
