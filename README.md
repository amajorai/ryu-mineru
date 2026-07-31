# ryu-mineru

MinerU for Ryu — document parsing via MinerU (opendatalab): the heaviest and highest-fidelity `document.parse` backend, built for scientific and scanned PDFs with layout detection and OCR.

> **The public home of `ryu-mineru`.** Source, builds, and releases live here —
> binaries for every platform are attached to each release.
>
> This tree is generated from the Ryu monorepo, so commits pushed here
> directly are replaced on the next sync. **Pull requests are welcome** —
> open them here and they are ported into the monorepo, then flow back out.
> Ryu as a whole: https://github.com/amajorai/ryu

## Source & build

The **source of record** for the universal Ryu TTS sidecar — a self-contained
Python HTTP front over several text-to-speech engines. Install its
dependencies (`pip install -r sidecar/requirements.txt`) and run
`python -m ryu_tts` from `sidecar/`; Core manages it as a sidecar in a
full Ryu install.

## License

Apache-2.0 — see [LICENSE](./LICENSE).

---

# MinerU — `document.parse` backend

Turns documents into markdown using [MinerU](https://github.com/opendatalab/MinerU)
(OpenDataLab). This is one of several interchangeable backends behind the swappable
`document.parse` capability: enable it, pick it in the provider selector, and
everything that ingests a document (Spaces, RAG, chat attachments) routes through
it. Nothing in Core is bound to it — the swap is manifest data.

> **Read the hardware section before installing.** MinerU is by far the heaviest
> of the four backends: 16 GB RAM minimum, ~20 GB of disk for its model set, and
> a GPU if you want it to be quick. It is the one users install and are surprised
> by. If you mostly attach ordinary PDFs and DOCX files, `markitdown` or
> `unstructured` will serve you better and cost you almost nothing.

## What it is good at

**Scientific and scanned PDFs.** This is the whole reason to pick it.

| MinerU does | Which matters when |
| --- | --- |
| Reconstructs **reading order** from a layout model | a two-column paper. Naive extractors interleave the columns line by line and the model reads nonsense |
| Converts **formulas to LaTeX** (`$…$`, `$$…$$`) | anything with maths. A formula extracted as a soup of glyphs is worse than no formula |
| Converts **tables to HTML** | financial tables and results tables survive into chunking as tables |
| **OCRs scanned pages** | a photographed or faxed document, where every other backend returns an empty string |
| Drops headers, footers and page numbers | they are chunk noise that matches nothing |

Output is markdown already — no element-to-markdown rendering step, unlike the
`unstructured` backend.

## What it costs

**Hardware, and this is the honest headline.** Upstream's own floors:

| | Minimum | Recommended |
| --- | --- | --- |
| Python | 3.10 | 3.10–3.13 |
| RAM | **16 GB** | 32 GB+ |
| Disk | **20 GB** (models) | more |
| GPU (pipeline backend) | 4 GB VRAM | more |
| GPU (VLM / hybrid backends) | 8 GB VRAM | more |
| GPU architecture | Volta or newer, or Apple Silicon | — |

**CPU-only works**, through the `pipeline` backend, and is the default this
sidecar ships (`RYU_MINERU_BACKEND=pipeline`). Note that this is *not* MinerU's
own default — MinerU 3.x defaults to `hybrid-engine`, which wants a GPU — so the
sidecar always passes `-b` explicitly rather than inheriting a default that would
fail or crawl on a laptop. Expect minutes per document on CPU; the per-parse
timeout is 600 s for that reason.

`/capability` reports this host's actual RAM and free disk against those floors,
and a parse on an under-spec machine attaches a warning to its result rather than
being refused — an underpowered host succeeding slowly beats a gate that guesses
wrong.

**Install weight.** `mineru[all]` (the manifest's `pyproject_extra`) pulls torch,
the layout/formula/table model loaders, and the VLM stack. `mineru[core]` is the
pipeline backend only and is much cheaper. **Models are not in the install** —
they download from Hugging Face on the first parse, into `HF_HOME` (the manifest
pins it under `${RYU_DIR}/models/hf`). The first parse on a fresh node therefore
takes minutes longer than every parse after it, and needs network.

> **Pre-warm the models.** The download *and* the ~45 s model init both come out
> of the job's 600 s budget, so on a slow link the **first** parse can time out,
> get killed, and retry from scratch forever while every later parse would have
> been fine. Run `mineru-models-download` (it ships in the sidecar venv's `bin/`)
> once after enabling the app, or raise `RYU_MINERU_TIMEOUT_SECS` for the first
> document. The `timeout` job error names this cause first, because it is the
> likeliest one.

**System dependencies: almost none.** This is where MinerU beats `unstructured`,
which shells out to four binaries pip cannot install. MinerU 3.x reads OOXML
(`.docx`, `.pptx`, `.xlsx`) natively. Only MinerU **2.x** needed LibreOffice for
those, and the sidecar reports it as a missing dependency on 2.x only — telling a
3.x user to `brew install libreoffice` would be noise.

**Format coverage is narrower than `unstructured`.** MinerU reads PDFs, page
images and OOXML Office. It does **not** read legacy binary Office (`.doc`,
`.ppt`, `.xls`), email (`.msg`, `.eml`), EPUB, or RTF. Those go to a broader
backend. Archives (`.zip`, `.tar[.gz|.bz2|.xz]`) are expanded and parsed member by
member, each nested under its own filename heading.

## Choosing between backends

Pick MinerU when the corpus is papers, reports with real tables, or scans, and
fidelity is worth the install. Pick `markitdown` when you want something small
that works everywhere. Pick `unstructured` when the corpus is a fifteen-year-old
shared drive full of `.doc` and `.msg`. Pick `docling` for a middle ground.

## HTTP contract

Reachable at `/api/ext/com.ryu.mineru/*`. Every path below is declared in
`manifest.json`; an undeclared path is refused with a 404 at the proxy before it
reaches this process.

```
GET    /health          -> { ok, backend, available, library_version, missing_dependencies }
GET    /capability      -> { capability, backend, formats, system_dependencies, limits, hardware }
POST   /parse           -> 202 { job_id, status }
GET    /jobs            -> { jobs: [ snapshot without result ] }
GET    /jobs/{job_id}   -> snapshot (result present once succeeded)
DELETE /jobs/{job_id}   -> snapshot (kills the running MinerU process)
```

`POST /parse` takes exactly one of `path` (absolute, confined to
`RYU_MINERU_ROOTS`) or `content_base64` + `filename`, plus optional `options`:

| Option | Values | Notes |
| --- | --- | --- |
| `backend` | `pipeline`, `vlm-engine`, `hybrid-engine`, `vlm-http-client`, `hybrid-http-client` (3.x); the 2.x `vlm-transformers` / `vlm-sglang-*` / `vlm-vllm-*` names are also accepted | which names actually work depends on the installed MinerU |
| `method` | `auto`, `txt`, `ocr` | pipeline/hybrid only |
| `effort` | `medium`, `high` | hybrid backends only |
| `lang` | MinerU's OCR language codes | improves OCR accuracy |
| `device` | `cpu`, `cuda[:n]`, `mps`, `npu`, `xpu` | passed as `MINERU_DEVICE_MODE`, which is where MinerU reads it — it has no device flag |
| `formula`, `table` | bool | on by default |
| `start_page`, `end_page` | int, 0-based | |

Unknown option keys — and unknown *values* — are ignored rather than rejected: a
hint one backend understands must not fail a parse on another.

A succeeded job's `result` is:

```jsonc
{
  "backend": "mineru",
  "backend_version": "3.4.4",
  "markdown": "# Quarterly Report\n\n$$E = mc^2$$\n\n<table>…</table>",  // primary payload
  "text": "Quarterly Report …",                                          // markup-free fallback
  "warnings": [ "this host has 8.0 GB of RAM; MinerU's stated minimum is 16 GB…" ],
  "truncated": false,
  "metadata": { "filename": "q3.pdf", "page_count": 18, "sources": ["q3.pdf"], "mineru_backend": "pipeline" }
}
```

Failed jobs carry `error`, `error_code` (`library_missing`, `missing_dependency`,
`unsupported_format`, `parse_failed`, `input_rejected`, `timeout`) and
`missing_dependencies`.

### Why submit-and-poll rather than one request

The ext-proxy's activity guard drops as soon as response headers arrive, so a
`lazy` + `idle_stop_secs` sidecar can be reaped **mid-request**. A ten-minute OCR
parse behind a single HTTP call is therefore killable. Every parse is a job:
`POST /parse` answers 202 immediately and the caller polls, which re-arms the
guard on each hit. This matters more here than for any sibling backend, because
MinerU's *first* run also downloads its models.

## Security posture

- **Fail-closed bearer.** `RYU_EXT_TOKEN` is read at import time and compared with
  `hmac.compare_digest`. No token configured means *reject everything*. `/health`
  is exempt on **GET only**.
- **Path confinement.** A parse input is resolved through symlinks and *then*
  required to live under `RYU_MINERU_ROOTS` (default `${RYU_DIR}`). Without the
  post-resolution check, a symlink planted in the blob dir turns this service into
  an arbitrary-file-read primitive.
- **Archive safety.** Absolute member names, `..` segments, and
  symlink/hardlink/device members are refused outright — not sanitised. Member
  count and expanded bytes are capped, and a member larger than its declared size
  aborts the extraction.
- **No shell.** The CLI is invoked as an argv list with `shell=False`; the input
  path never reaches a shell parser. The bearer token is stripped from the child's
  environment.
- **Bounded everything.** Input bytes, output bytes, wall-clock per parse,
  concurrent workers, and retained jobs all have caps (see
  `sidecar/ryu_mineru/limits.py`); each is env-overridable and reported by
  `/capability`. The output cap is **one budget for the whole result**, not one
  per field: `markdown` takes the ceiling and `text` takes the remainder, because
  8 MiB of each would put a 16 MiB body against the proxy's 10 MiB limit and make
  a large result unreadable rather than truncated. An archive stops accumulating
  members at the same ceiling and says how many it left out.
- The sidecar itself makes **no network calls**. MinerU downloads models on first
  use, which is the one and only egress.

### Timeout enforcement — stricter than the in-process backends

`unstructured` parses in-process, and CPython cannot kill a thread, so its
watchdog can only stop *waiting* on a runaway parse. MinerU is a subprocess, so
this sidecar actually kills it:

- the child runs in its **own session**, making it a process-group leader;
- on timeout or on `DELETE /jobs/{id}`, the whole **process group** gets SIGTERM,
  then SIGKILL after `RYU_MINERU_KILL_GRACE_SECS`, then is reaped;
- MinerU's CLI starts a temporary local API service that puts itself in a
  *different* process group. SIGTERM to the CLI normally shuts it down cleanly,
  but "normally" is not "always", so descendants are snapshotted before signalling
  and any escaped group is swept afterwards. A surviving one would be an HTTP
  server holding gigabytes of model state.
- both output streams go to a file, never a pipe. `Popen(stdout=PIPE)` + `wait()`
  deadlocks once the pipe buffer fills — and MinerU is chatty. That bug passes a
  small-fixture test and hangs on a real 300-page PDF.

A cancelled or timed-out parse leaves nothing behind. On a machine that meets the
16 GB floor with nothing to spare, an orphan is not a leak, it is an outage.

## Configuration

| Env | Default | Meaning |
| --- | --- | --- |
| `RYU_MINERU_PORT` | 8096 | Bind port. Core injects the **profile-shifted** value (dev profile = +1000, so 9096). |
| `RYU_MINERU_HOST` | `127.0.0.1` | Bind host. Loopback only — this process reads local files. |
| `RYU_EXT_TOKEN` | — | Shared bearer. Unset ⇒ every route except `GET /health` returns 401. |
| `RYU_MINERU_ROOTS` | `${RYU_DIR}` | `os.pathsep`-separated roots a parse input may live under. |
| `RYU_MINERU_WORKDIR` | temp dir | Staged uploads, CLI output trees, model cache. |
| `RYU_MINERU_BACKEND` | `pipeline` | Node default parse backend. **Do not set this to a VLM/hybrid backend on a machine without a GPU.** |
| `RYU_MINERU_DEVICE` | unset | `cpu`, `cuda[:n]`, `mps`, … Exported to the child as `MINERU_DEVICE_MODE`. |
| `RYU_MINERU_LANG` | unset | Default OCR language. |
| `RYU_MINERU_CLI` | unset | Absolute path to a `mineru` executable, for hosts that installed it outside the sidecar venv. |
| `RYU_MINERU_MAX_INPUT_BYTES` | 200 MiB | Largest input file or archive. |
| `RYU_MINERU_MAX_OUTPUT_BYTES` | 8 MiB | Result cap; over it the payload is clipped and `truncated` is true. |
| `RYU_MINERU_TIMEOUT_SECS` | 600 | Wall-clock ceiling per job, model download included. |
| `RYU_MINERU_KILL_GRACE_SECS` | 5 | SIGTERM→SIGKILL grace when reaping a parse. |
| `RYU_MINERU_MAX_WORKERS` | 2 | Concurrent parses. Each is a separate model-loading process. |
| `RYU_MINERU_MAX_JOBS` | 64 | Retained jobs before the oldest terminal ones are evicted. |
| `RYU_MINERU_MAX_ARCHIVE_MEMBERS` | 512 | Members expanded from one archive. |
| `RYU_MINERU_MAX_ARCHIVE_BYTES` | 512 MiB | Total expanded archive bytes. |

## Developing

```bash
cd sidecar
uv venv --python 3.11 .venv && source .venv/bin/activate
pip install -e ".[core]"        # pipeline backend, CPU-capable
# pip install -e ".[all]"       # everything, including the VLM/hybrid backends
python smoke_test.py            # contract tests; runs with or without MinerU
python -m ryu_mineru            # serve on 127.0.0.1:8096
```

Upstream's own install route is `pip install uv && uv pip install -U "mineru[all]"`.
Core does not use `uv` — `external_runtime.rs` builds a venv and pip-installs the
manifest's `pyproject_extra` — so the extras above are the supported path here.

`smoke_test.py` covers the fail-closed bearer, the open `GET /health` (and the
closed `POST /health`), `/capability` answering without MinerU, a full parse
round-trip, path-confinement rejection (outside-root, `..`, escaping symlink),
archive traversal rejection, the shared output budget, and — by PID, not by
status — that a timed-out parse and a cancelled parse both leave no live child
(verified against the real MinerU CLI too, including the API service it starts in
its own process group). The parse path runs against a CLI
stub by default so it needs no models; set `RYU_MINERU_SMOKE_REAL=1` with MinerU
installed to also parse a generated PDF through the real binary.
