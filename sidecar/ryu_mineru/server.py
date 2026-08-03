"""Ryu MinerU sidecar — HTTP front for the `document.parse` capability.

The contract Core drives (every path below is declared in `manifest.json`; an
undeclared path 404s at the ext-proxy before it ever reaches this process):

    GET    /health          -> { ok, backend, available, missing_dependencies }
    GET    /capability      -> { backend, formats, limits, system_dependencies }
    POST   /parse           -> 202 { job_id, status }        (never blocks)
    GET    /jobs            -> { jobs: [ JobSnapshot ] }      (no results)
    GET    /jobs/{job_id}   -> JobSnapshot                    (result when done)
    DELETE /jobs/{job_id}   -> JobSnapshot                    (kills the process)

Submit-then-poll is not a style choice: the ext-proxy's activity guard drops when
response headers arrive, so a single long-lived parse request on a `lazy` +
`idle_stop_secs` sidecar can be reaped mid-flight. Polling re-arms the guard. It
matters more here than for any sibling backend — MinerU's first run downloads its
model set, and even a warm parse of a scanned document runs for minutes.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import BACKEND, __version__
from .cli import BACKENDS, EFFORTS, METHODS, cli_path, configured_backend, mineru_version
from .deps import ALL_DEPS, hardware
from .formats import missing_dependencies, office_support, supported_extensions
from .jobs import STORE
from .limits import (
    MAX_INPUT_BYTES,
    MAX_JOBS,
    MAX_OUTPUT_BYTES,
    MAX_WORKERS,
    TIMEOUT_SECS,
)
from .paths import InputError, resolve_input, workdir

app = FastAPI(title="Ryu MinerU Sidecar", version=__version__)

# Shared-secret bearer Core stamps on every proxied hop and injects at spawn
# (`RYU_EXT_TOKEN`). FAIL-CLOSED for every route except GET /health: no token
# configured => reject all. Without it, any local process (or any web page that
# can reach loopback) could hand this sidecar a path and read the file back as
# "parsed text" — an arbitrary-file-read primitive.
_EXPECTED_TOKEN = (os.environ.get("RYU_EXT_TOKEN") or "").strip()


@app.middleware("http")
async def _require_ext_token(request: Request, call_next):
    # GET only: a POST to /health must not become an unauthenticated hole if the
    # route ever grows a body.
    if request.url.path == "/health" and request.method == "GET":
        return await call_next(request)
    header = request.headers.get("authorization", "")
    presented = header[len("Bearer ") :].strip() if header.startswith("Bearer ") else ""
    if not (_EXPECTED_TOKEN and hmac.compare_digest(presented, _EXPECTED_TOKEN)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


class ParseRequest(BaseModel):
    path: Optional[str] = Field(
        None,
        description="Absolute path to the document, confined to RYU_MINERU_ROOTS.",
    )
    content_base64: Optional[str] = Field(
        None, description="Inline document bytes, for callers with no shared filesystem."
    )
    filename: Optional[str] = Field(
        None, description="Name (extension matters) for `content_base64` input."
    )
    options: dict[str, Any] = Field(
        default_factory=dict,
        description="Backend hints: backend (pipeline|vlm-*|hybrid-*), method "
        "(auto|txt|ocr), effort (medium|high), lang, device, formula, table, "
        "start_page, end_page. Unknown keys — and unknown values — are ignored, "
        "never an error: a hint one backend understands must not fail on another.",
    )


def _limits() -> dict[str, Any]:
    return {
        "max_input_bytes": MAX_INPUT_BYTES,
        "max_output_bytes": MAX_OUTPUT_BYTES,
        "timeout_secs": TIMEOUT_SECS,
        "max_workers": MAX_WORKERS,
        "max_jobs": MAX_JOBS,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "version": __version__,
        "backend": BACKEND,
        # `available` is the honest answer to "can this parse anything right
        # now": the `mineru` executable must exist. A missing LibreOffice narrows
        # the format list rather than disabling the backend.
        "available": cli_path() is not None,
        "library_version": mineru_version(),
        "missing_dependencies": missing_dependencies(),
    }


@app.get("/capability")
def capability() -> dict[str, Any]:
    root = str(workdir())
    return {
        "capability": "document.parse",
        "backend": BACKEND,
        "version": __version__,
        "available": cli_path() is not None,
        "library_version": mineru_version(),
        "formats": supported_extensions(),
        "system_dependencies": {dep.key: dep.present() for dep in ALL_DEPS},
        "missing_dependencies": missing_dependencies(),
        "limits": _limits(),
        # MinerU-specific, and the reason a user picks or un-picks this backend:
        # which parse backend this node runs, and whether the host clears the
        # bar upstream sets. Unknown keys in a capability payload are ignored by
        # Core, which reads `formats`, `available` and `missing_dependencies`.
        "options": {
            "backends": list(BACKENDS),
            "backend": configured_backend(),
            "methods": list(METHODS),
            "efforts": list(EFFORTS),
            "office_support": office_support(),
        },
        "hardware": hardware(root),
    }


def _staging_dir() -> Path:
    """Where inline uploads land. Core points this at `${RYU_DIR}/cache/mineru`."""
    staging = workdir() / "uploads"
    staging.mkdir(parents=True, exist_ok=True)
    return staging


def _materialise_inline(req: ParseRequest) -> Path:
    """Write `content_base64` to a scratch file we own, returning its path."""
    try:
        raw = base64.b64decode(req.content_base64 or "", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InputError(f"`content_base64` is not valid base64: {exc}") from exc
    if not raw:
        raise InputError("`content_base64` decoded to zero bytes")
    if len(raw) > MAX_INPUT_BYTES:
        raise InputError(
            f"inline input is {len(raw)} bytes, over the {MAX_INPUT_BYTES}-byte limit"
        )
    # Only the extension is taken from the caller's filename — the rest of the
    # name is ours, so a crafted `filename` cannot steer the write anywhere.
    suffix = Path((req.filename or "document.pdf").replace("\\", "/")).suffix[:16]
    handle, tmp_path = tempfile.mkstemp(suffix=suffix or ".pdf", dir=str(_staging_dir()))
    with os.fdopen(handle, "wb") as dst:
        dst.write(raw)
    return Path(tmp_path)


@app.post("/parse")
def parse(req: ParseRequest) -> JSONResponse:
    if bool(req.path) == bool(req.content_base64):
        return JSONResponse(
            {"error": "provide exactly one of `path` or `content_base64`"},
            status_code=400,
        )
    try:
        target = _materialise_inline(req) if req.content_base64 else resolve_input(req.path or "")
    except InputError as exc:
        return JSONResponse(
            {"error": str(exc), "error_code": "input_rejected"}, status_code=400
        )

    job = STORE.submit(target, req.options or {})
    # 202: the parse has been accepted, not performed. The caller polls
    # /jobs/{job_id} — see the module docstring for why this may not be one
    # long request.
    return JSONResponse({"job_id": job.id, "status": job.status}, status_code=202)


@app.get("/jobs")
def list_jobs() -> dict[str, Any]:
    # Results are omitted here on purpose: a listing that inlined every parsed
    # document would be megabytes and would blow the proxy's body cap.
    return {"jobs": [job.snapshot(include_result=False) for job in STORE.list()]}


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> JSONResponse:
    job = STORE.get(job_id)
    if job is None:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return JSONResponse(job.snapshot())


@app.delete("/jobs/{job_id}")
def cancel_job(job_id: str) -> JSONResponse:
    # Unlike the in-process backends, this genuinely stops the work: the MinerU
    # process group is signalled before the response is written.
    job = STORE.cancel(job_id)
    if job is None:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return JSONResponse(job.snapshot(include_result=False))
