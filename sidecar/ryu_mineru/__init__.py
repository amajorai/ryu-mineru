"""Ryu MinerU sidecar — a Core-managed document-parsing runtime.

Wraps [MinerU](https://github.com/opendatalab/MinerU) behind the swappable
`document.parse` capability contract: submit a parse, get a `job_id` back
immediately, poll for the result. Core owns lifecycle, storage, chunking and
embedding; this process only turns bytes on disk into markdown.

Two things make this backend different from its three siblings:

* **MinerU is a CLI, not a library.** Every parse shells out to
  `mineru -p <input> -o <outdir>` and reads the produced markdown back. So the
  per-parse timeout is enforced on a *process*, not on a thread — and unlike a
  Python-level watchdog it can, and does, actually kill the work. See
  `runner.py`.
* **It is the heaviest of the four.** Layout, formula and table models are
  downloaded on first use and loaded per parse; 16 GB RAM is the floor and a
  4 GB-VRAM GPU is what makes it pleasant. `README.md` is blunt about this
  because it is the surprise users will hit.

Why job-id + poll rather than one long request: the ext-proxy's activity guard
drops as soon as response headers arrive, so a `lazy` + `idle_stop_secs` sidecar
can be reaped *mid-request*. A ten-minute OCR parse behind a single HTTP call is
therefore killable; a 202 + poll loop is not (each poll re-arms the guard).
"""

from __future__ import annotations

__version__ = "0.1.0"

# Default HTTP port. Core pins the real (profile-shifted) port at spawn via
# RYU_MINERU_PORT — under the dev profile every port is +1000, so this constant
# is only the standalone/bare-`python -m` fallback.
DEFAULT_PORT = 8096

# Backend id as it appears in `document.parse` provider binding + /capability.
BACKEND = "mineru"
