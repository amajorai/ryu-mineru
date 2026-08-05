"""The parse itself: a file path in, a `document.parse` result out.

Everything that can go wrong here is reported as a typed error the job carries,
never as an exception that kills the worker or an empty result that looks like a
blank document:

  * `library_missing`      — the `mineru` CLI is not installed in this venv
  * `missing_dependency`   — a native tool this format needs is absent
  * `unsupported_format`   — MinerU has no reader for this extension
  * `parse_failed`         — the CLI exited non-zero, or wrote no markdown
  * `input_rejected`       — path confinement / archive safety refused the input
  * `timeout`              — the job's budget ran out; the child has been killed

Two shapes here are specific to a CLI backend and are the reason this file does
not look like `unstructured`'s:

**Output discovery is a search, not a formula.** MinerU's output tree has moved
between releases (`<out>/<stem>/auto/<stem>.md` at the time of writing). Hardcoding
that path means a MinerU upgrade turns every parse into "no text found" — the
exact silent failure this capability exists to kill. So we glob for markdown under
the output dir we created and pick deterministically, and *no markdown at all* is
a loud `parse_failed` carrying the child's log tail.

**Success with nothing in it is a failure.** Core's `normalize_job` would demote
an empty `succeeded` anyway; doing it here means the job also carries the reason.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

from . import BACKEND
from .cli import build_argv, child_env, cli_path, mineru_version, resolve_backend
from .deps import LIBREOFFICE, hardware_warnings
from .formats import (
    ARCHIVE_EXTENSIONS,
    CORE_EXTENSIONS,
    OFFICE_EXTENSIONS,
    UNAVAILABLE,
    office_support,
)
from .limits import MAX_OUTPUT_BYTES
from .markdown import to_plain_text, truncate
from .paths import InputError, is_archive, safe_extract, workdir
from .runner import ChildFailed, ProcessRunner, StepTimeout

# A `_content_list.json` this large is not worth reading for a page count.
MAX_SIDECAR_JSON_BYTES = 32 * 1024 * 1024

# Below this much leftover budget, the plain-text mirror is dropped entirely
# rather than shipped as a meaningless opening fragment.
MIN_TEXT_BUDGET_BYTES = 64 * 1024


class ParseError(RuntimeError):
    """A parse failure with a machine-readable code and a human-readable fix."""

    def __init__(self, code: str, message: str, *, missing: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.missing = missing or []


def _preflight(path: Path) -> None:
    """Refuse a format this host cannot read, before spending a process on it."""
    ext = path.suffix.lower()
    if ext in CORE_EXTENSIONS or ext in ARCHIVE_EXTENSIONS:
        return
    if ext in OFFICE_EXTENSIONS:
        if office_support() != UNAVAILABLE:
            return
        raise ParseError(
            "missing_dependency",
            f"{LIBREOFFICE.message()} Upgrading to MinerU 3.x also fixes this — it "
            "reads OOXML without a converter.",
            missing=[LIBREOFFICE.key],
        )
    raise ParseError(
        "unsupported_format",
        f"MinerU has no reader for `{ext or path.name}` — it parses PDFs, page images "
        "and OOXML Office documents. Legacy binary Office (.doc/.ppt/.xls), email and "
        "EPUB need a broader `document.parse` backend.",
    )


def _require_cli() -> Path:
    cli = cli_path()
    if cli is None:
        raise ParseError(
            "library_missing",
            "the `mineru` CLI is not installed in this sidecar's venv — install it "
            'with `pip install -U "mineru[core]"` (or set RYU_MINERU_CLI to an '
            "existing install)",
        )
    return cli


def _pick_markdown(outdir: Path, stem: str) -> Optional[Path]:
    """Deterministically choose the markdown MinerU wrote.

    Preference order: a file whose stem matches the input's, then the largest,
    then lexicographic — so two runs over the same document always pick the same
    file even when MinerU emits several.
    """
    candidates = [p for p in outdir.rglob("*.md") if p.is_file()]
    if not candidates:
        return None
    wanted = stem.lower()

    def rank(path: Path) -> tuple[int, int, str]:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return (0 if path.stem.lower() == wanted else 1, -size, str(path))

    return sorted(candidates, key=rank)[0]


def _page_count(outdir: Path) -> Optional[int]:
    """Best-effort page count from MinerU's `*_content_list.json`.

    Purely decorative provenance: a missing, oversized or reshaped file yields
    None rather than failing a parse that otherwise worked.
    """
    for candidate in outdir.rglob("*_content_list.json"):
        try:
            if candidate.stat().st_size > MAX_SIDECAR_JSON_BYTES:
                continue
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, list):
            continue
        pages = [
            item["page_idx"]
            for item in payload
            if isinstance(item, dict) and isinstance(item.get("page_idx"), int)
        ]
        if pages:
            return max(pages) + 1
    return None


def _read_markdown(path: Path) -> str:
    """Read a result file, capped. MinerU's own output is not trusted for size."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_OUTPUT_BYTES + 1)
    except OSError as exc:
        raise ParseError("parse_failed", f"could not read MinerU's output: {exc}") from exc
    return raw.decode("utf-8", errors="replace")


def _run_mineru(source: Path, options: dict[str, Any], runner: ProcessRunner) -> tuple[str, Optional[int]]:
    """One CLI invocation. Returns `(markdown, page_count)`.

    The scratch tree is ours and is removed afterwards: MinerU writes cropped
    figure images and three JSON sidecars next to the markdown, and a node that
    parses a hundred documents should not accumulate a hundred of those.
    """
    cli = _require_cli()
    scratch = Path(tempfile.mkdtemp(prefix="parse-", dir=str(workdir())))
    outdir = scratch / "out"
    outdir.mkdir(parents=True, exist_ok=True)
    log_path = scratch / "mineru.log"
    try:
        argv = build_argv(cli, source, outdir, options)
        try:
            runner.run(argv, cwd=scratch, env=child_env(workdir(), options), log_path=log_path)
        except StepTimeout as exc:
            raise ParseError(
                "timeout",
                "the parse exceeded its time budget and the MinerU process was "
                "killed. On a fresh node the likeliest cause is the FIRST parse "
                "still downloading models — pre-warm them with "
                "`mineru-models-download` and retry. Otherwise raise "
                "RYU_MINERU_TIMEOUT_SECS for very large scans, or use the "
                "`pipeline` backend on a machine without a GPU",
            ) from exc
        except ChildFailed as exc:
            raise ParseError(
                "parse_failed",
                f"MinerU exited with code {exc.code} on `{source.name}`"
                + (f": {exc.output[-800:]}" if exc.output else ""),
            ) from exc

        found = _pick_markdown(outdir, source.stem)
        if found is None:
            tail = ""
            try:
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-800:]
            except OSError:
                pass
            raise ParseError(
                "parse_failed",
                f"MinerU completed but wrote no markdown for `{source.name}`"
                + (f": {tail}" if tail.strip() else ""),
            )
        return _read_markdown(found), _page_count(outdir)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _parse_archive(
    path: Path, options: dict[str, Any], runner: ProcessRunner
) -> tuple[str, list[str], list[str], Optional[int], bool]:
    """Expand an archive and parse every member MinerU can read.

    Returns `(markdown, warnings, sources, page_count, over_budget)`.

    One unreadable member must not sink the whole archive, so per-member failures
    become warnings and the rest of the documents still come back. Two budgets are
    shared across the whole archive rather than handed out per member:

    * **Time.** The runner's deadline is job-wide, so 512 members share one 600 s
      budget rather than getting one each. The member that exhausts it aborts the
      job — everything parsed so far is discarded rather than silently passed off
      as the whole archive.
    * **Bytes.** Accumulation stops at `MAX_OUTPUT_BYTES` and says how many
      members were left out. Without this, a zip of a few hundred PDFs builds a
      string bounded only by `MAX_ARCHIVE_BYTES` — hundreds of megabytes held in
      memory to then be clipped by a factor of fifty.
    """
    sections: list[str] = []
    warnings: list[str] = []
    sources: list[str] = []
    pages: Optional[int] = None
    used = 0
    over_budget = False
    with tempfile.TemporaryDirectory(prefix="ryu-mineru-", dir=str(workdir())) as scratch:
        root = Path(scratch).resolve()
        try:
            members = safe_extract(path, root)
        except InputError as exc:
            raise ParseError("input_rejected", str(exc)) from exc
        ordered = sorted(members)
        for index, member in enumerate(ordered):
            runner.checkpoint()
            relative = str(member.relative_to(root))
            try:
                _preflight(member)
                markdown, member_pages = _run_mineru(member, options, runner)
            except ParseError as exc:
                if exc.code == "timeout":
                    raise
                warnings.append(f"{relative}: {exc}")
                continue
            if not markdown.strip():
                warnings.append(f"{relative}: parsed to an empty document")
                continue
            # Each member nests under its own filename heading; without it an
            # archive renders as a flat run of `#` headings with no way to tell
            # where one document ends.
            section = f"# {relative}\n\n{markdown.strip()}"
            cost = len(section.encode("utf-8")) + 2  # the joining blank line
            if used + cost > MAX_OUTPUT_BYTES:
                skipped = len(ordered) - index
                warnings.append(
                    f"output budget reached: {skipped} of {len(ordered)} archive "
                    "members are not included in this result"
                )
                over_budget = True
                break
            used += cost
            sources.append(relative)
            pages = (pages or 0) + (member_pages or 0) or None
            sections.append(section)
    return "\n\n".join(sections), warnings, sources, pages, over_budget


def parse_file(
    path: Path, options: dict[str, Any] | None, runner: ProcessRunner
) -> dict[str, Any]:
    """Parse one file (or one archive of files) into a `document.parse` result.

    The shape is the contract's: `markdown` is the primary payload, `text` the
    markup-free fallback, and `truncated` says whether the byte budget clipped the
    output.
    """
    options = options or {}
    warnings = hardware_warnings(str(workdir()))

    over_budget = False
    if is_archive(path):
        raw_markdown, member_warnings, sources, pages, over_budget = _parse_archive(
            path, options, runner
        )
        warnings.extend(member_warnings)
    else:
        _preflight(path)
        raw_markdown, pages = _run_mineru(path, options, runner)
        sources = [path.name]

    if not raw_markdown.strip():
        # "Succeeded with nothing in it" is the silent drop wearing a different
        # hat. Core would demote it anyway; failing here means the job also says
        # why.
        raise ParseError(
            "parse_failed",
            f"MinerU extracted no text from `{path.name}` — if this is a scanned "
            "document, check that the `pipeline` backend's OCR models finished "
            "downloading",
        )

    # ONE byte budget for the whole result, not one per field. `MAX_OUTPUT_BYTES`
    # is 8 MiB precisely so the response stays under the ext-proxy's 10 MiB body
    # cap (§3.6) — and a cap the proxy enforces makes a large result *unreadable*
    # rather than truncated. Clipping `markdown` and `text` to 8 MiB each would
    # put a 16 MiB body on the wire and lose the whole document.
    #
    # `markdown` gets the ceiling and `text` gets the remainder, because Core's
    # `normalize_job` reads `result.markdown` and nothing else. Sacrificing the
    # plain-text mirror of a document costs a caller nothing it cannot rebuild.
    markdown, whole_md = truncate(raw_markdown, MAX_OUTPUT_BYTES)
    remaining = MAX_OUTPUT_BYTES - len(markdown.encode("utf-8"))
    if remaining >= MIN_TEXT_BUDGET_BYTES:
        text, whole_text = truncate(to_plain_text(raw_markdown), remaining)
    else:
        text, whole_text = "", False
    if not whole_text:
        warnings.append(
            "the plain-text mirror was clipped to keep the response under the "
            "8 MiB budget; `markdown` is the complete payload"
        )
    return {
        "backend": BACKEND,
        "backend_version": mineru_version(),
        "markdown": markdown,
        "text": text,
        "warnings": warnings,
        # `truncated` is about the DOCUMENT, so it tracks `markdown` and the
        # archive members that did not fit. A clipped `text` mirror is reported
        # as a warning instead — flagging the document truncated because a
        # derived field was shortened would send callers re-parsing for nothing.
        "truncated": (not whole_md) or over_budget,
        "metadata": {
            "filename": path.name,
            "page_count": pages,
            "sources": sources,
            "mineru_backend": resolve_backend(options),
        },
    }


__all__ = ["ParseError", "parse_file"]
# `runner.Cancelled` deliberately propagates through this module untouched: a
# cancelled job is not a parse failure, and `JobStore` has already written the
# terminal state.
