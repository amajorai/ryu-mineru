"""Smoke test: the contract holds with or without MinerU installed.

Covers the acceptance criteria that need no model download:
  - an unauthenticated request is rejected (fail-closed bearer gate)
  - GET /health is open, POST /health is NOT
  - /capability answers even when MinerU is absent
  - POST /parse returns 202 + a job_id immediately, and the job reaches a
    terminal state (`library_missing` with no CLI installed — never a hang)
  - a full parse round-trip against a CLI stub that behaves like MinerU: argv
    construction, output-tree discovery, markdown read-back, job success
  - **the timeout actually kills the child**, and so does DELETE /jobs/{id} —
    asserted by PID, not by status. This is the one place this backend must
    behave differently from its in-process siblings, so it is the one thing this
    test proves rather than assumes.
  - path confinement rejects `..`, absolute paths outside the roots, and
    symlinks pointing out of the allowed roots
  - archive expansion rejects traversal members

A parse through the *real* MinerU downloads a multi-gigabyte model set on first
run, so it is opt-in: set `RYU_MINERU_SMOKE_REAL=1` with `mineru` installed to
run it against a generated one-page PDF. Without it the test prints which mode it
ran in and never pretends a stubbed parse was a real one.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import time
import zipfile
from pathlib import Path

# The server reads RYU_EXT_TOKEN at import time for its fail-closed auth gate;
# set it before importing `app` and present it as the bearer on every request.
os.environ.setdefault("RYU_EXT_TOKEN", "smoke-token")

# Confine parse inputs to this run's scratch dir so the confinement test has a
# real boundary to cross. Must also be set before the modules read it.
_SCRATCH = Path(tempfile.mkdtemp(prefix="ryu-mineru-smoke-"))
_ROOT = _SCRATCH / "root"
_ROOT.mkdir()
os.environ["RYU_MINERU_ROOTS"] = str(_ROOT)
os.environ["RYU_MINERU_WORKDIR"] = str(_SCRATCH / "work")

from fastapi.testclient import TestClient  # noqa: E402

from ryu_mineru import cli as cli_mod  # noqa: E402
from ryu_mineru.jobs import STORE  # noqa: E402
from ryu_mineru.markdown import to_plain_text, truncate  # noqa: E402
from ryu_mineru.parser import ParseError, _run_mineru  # noqa: E402
from ryu_mineru.paths import InputError, safe_extract  # noqa: E402
from ryu_mineru.runner import ProcessRunner  # noqa: E402
from ryu_mineru.server import app  # noqa: E402

TOKEN = os.environ["RYU_EXT_TOKEN"]
client = TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})
anon = TestClient(app)

TERMINAL = {"succeeded", "failed", "cancelled"}

# A CLI stub that writes MinerU's output tree (`<out>/<stem>/auto/<stem>.md`) and
# can be told to hang. Standing in for the real binary is what makes the parse,
# timeout and cancel paths testable on a laptop with no GPU and no 20 GB of
# models.
_SHIM_SOURCE = '''#!@PYTHON@
import argparse, json, os, pathlib, sys, time

parser = argparse.ArgumentParser()
parser.add_argument("-p")
parser.add_argument("-o")
args, _unknown = parser.parse_known_args()

pidfile = os.environ.get("RYU_SMOKE_PIDFILE")
if pidfile:
    pathlib.Path(pidfile).write_text(str(os.getpid()), encoding="utf-8")

sleep_for = float(os.environ.get("RYU_SMOKE_SHIM_SLEEP") or 0)
if sleep_for:
    time.sleep(sleep_for)

stem = pathlib.Path(args.p).stem
out = pathlib.Path(args.o) / stem / "auto"
out.mkdir(parents=True, exist_ok=True)
body = "# Ryu Document Parsing\\n\\nMinerU renders $E = mc^2$ as LaTeX.\\n"
bulk = int(os.environ.get("RYU_SMOKE_SHIM_BYTES") or 0)
if bulk:
    body = body + ("lorem ipsum dolor sit amet " * ((bulk // 27) + 1))
(out / (stem + ".md")).write_text(body, encoding="utf-8")
(out / (stem + "_content_list.json")).write_text(
    json.dumps([{"page_idx": 0}, {"page_idx": 1}]), encoding="utf-8"
)
'''


def _write_shim() -> Path:
    path = _SCRATCH / "mineru-shim"
    path.write_text(_SHIM_SOURCE.replace("@PYTHON@", sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return path


SHIM = _write_shim()


def _tiny_pdf() -> bytes:
    """A minimal, structurally valid one-page PDF with a line of text."""
    stream = b"BT /F1 14 Tf 20 150 Td (Ryu Document Parsing) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n".encode()
    out += b"%%EOF\n"
    return bytes(out)


def _await_terminal(job_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    snap: dict = {}
    while time.time() < deadline:
        snap = client.get(f"/jobs/{job_id}").json()
        if snap["status"] in TERMINAL:
            return snap
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} never reached a terminal state: {snap}")


def _pid_is_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def test_auth_is_fail_closed() -> None:
    assert anon.post("/parse", json={"path": "/x"}).status_code == 401
    assert anon.get("/jobs").status_code == 401
    assert anon.get("/capability").status_code == 401
    # /health is exempt on GET only.
    assert anon.get("/health").status_code == 200
    assert anon.post("/health").status_code == 401
    print("auth: unauthenticated rejected, GET /health open, POST /health closed")


def test_capability_answers_without_the_library() -> dict:
    cap = client.get("/capability").json()
    assert cap["capability"] == "document.parse", cap
    assert cap["backend"] == "mineru", cap
    assert ".pdf" in cap["formats"], cap
    assert cap["limits"]["timeout_secs"] > 0, cap
    assert cap["options"]["backend"] in cap["options"]["backends"], cap
    assert isinstance(cap["system_dependencies"], dict), cap
    ram = cap["hardware"]["total_ram_bytes"]
    print(
        f"capability: available={cap['available']} mineru={cap['library_version']} "
        f"backend={cap['options']['backend']} "
        f"ram={'unknown' if ram is None else f'{ram / 1024**3:.1f} GB'} "
        f"missing={cap['missing_dependencies']}"
    )
    return cap


def test_parse_without_a_cli(available: bool) -> None:
    """With no MinerU installed the job must fail cleanly, not hang or crash."""
    if available:
        print("parse: MinerU CLI present, skipping the library-missing path")
        return
    fixture = _ROOT / "missing-cli.pdf"
    fixture.write_bytes(_tiny_pdf())
    submitted = client.post("/parse", json={"path": str(fixture)})
    assert submitted.status_code == 202, submitted.text
    snap = _await_terminal(submitted.json()["job_id"])
    assert snap["status"] == "failed", snap
    assert snap["error_code"] == "library_missing", snap
    print(f"parse: no CLI installed, clean job error -> {snap['error'][:70]}...")


def test_parse_roundtrip_against_the_stub() -> None:
    """The whole pipeline, with the CLI stubbed: argv, discovery, read-back."""
    fixture = _ROOT / "paper.pdf"
    fixture.write_bytes(_tiny_pdf())
    os.environ["RYU_MINERU_CLI"] = str(SHIM)
    try:
        submitted = client.post("/parse", json={"path": str(fixture), "options": {}})
        assert submitted.status_code == 202, submitted.text
        snap = _await_terminal(submitted.json()["job_id"])
    finally:
        os.environ.pop("RYU_MINERU_CLI", None)
    assert snap["status"] == "succeeded", snap
    result = snap["result"]
    assert "Ryu Document Parsing" in result["markdown"], result["markdown"][:200]
    assert result["backend"] == "mineru", result
    assert result["truncated"] is False, result
    assert result["metadata"]["page_count"] == 2, result["metadata"]
    assert result["metadata"]["mineru_backend"] == "pipeline", result["metadata"]
    print(
        "parse: stubbed round-trip succeeded, "
        f"{len(result['markdown'])} md chars, page_count={result['metadata']['page_count']}"
    )


def test_inline_parse_accepted() -> None:
    import base64

    body = base64.b64encode(_tiny_pdf()).decode("ascii")
    os.environ["RYU_MINERU_CLI"] = str(SHIM)
    try:
        r = client.post("/parse", json={"content_base64": body, "filename": "note.pdf"})
        assert r.status_code == 202, r.text
        snap = _await_terminal(r.json()["job_id"])
    finally:
        os.environ.pop("RYU_MINERU_CLI", None)
    assert snap["status"] == "succeeded", snap
    print("inline: content_base64 accepted, parsed, and staged under our own name")


def test_timeout_kills_the_child() -> None:
    """The headline difference from the in-process backends: the child DIES.

    A watchdog that merely stops waiting would leave a MinerU process loading
    models — on a host that meets the 16 GB floor with nothing to spare, that is
    an outage, not a leak. Asserted by PID.
    """
    fixture = _ROOT / "slow.pdf"
    fixture.write_bytes(_tiny_pdf())
    pidfile = _SCRATCH / "shim.pid"
    if pidfile.exists():
        pidfile.unlink()
    os.environ["RYU_MINERU_CLI"] = str(SHIM)
    os.environ["RYU_SMOKE_SHIM_SLEEP"] = "300"
    os.environ["RYU_SMOKE_PIDFILE"] = str(pidfile)
    try:
        runner = ProcessRunner(time.time() + 2.0)
        started = time.time()
        try:
            _run_mineru(fixture, {}, runner)
            raise AssertionError("a 300s child was not stopped by a 2s budget")
        except ParseError as exc:
            assert exc.code == "timeout", exc.code
        elapsed = time.time() - started
    finally:
        for name in ("RYU_MINERU_CLI", "RYU_SMOKE_SHIM_SLEEP", "RYU_SMOKE_PIDFILE"):
            os.environ.pop(name, None)

    assert elapsed < 30, f"kill took {elapsed:.1f}s"
    pid = int(pidfile.read_text(encoding="utf-8").strip())
    assert _pid_is_gone(pid), f"MinerU child {pid} survived its own timeout"
    print(f"timeout: child pid {pid} killed after {elapsed:.1f}s, not orphaned")


def test_cancel_kills_the_child() -> None:
    """DELETE /jobs/{id} stops the work, not just the status field."""
    fixture = _ROOT / "cancel.pdf"
    fixture.write_bytes(_tiny_pdf())
    pidfile = _SCRATCH / "cancel.pid"
    if pidfile.exists():
        pidfile.unlink()
    os.environ["RYU_MINERU_CLI"] = str(SHIM)
    os.environ["RYU_SMOKE_SHIM_SLEEP"] = "300"
    os.environ["RYU_SMOKE_PIDFILE"] = str(pidfile)
    try:
        submitted = client.post("/parse", json={"path": str(fixture)})
        assert submitted.status_code == 202, submitted.text
        job_id = submitted.json()["job_id"]
        deadline = time.time() + 30
        while not pidfile.exists() and time.time() < deadline:
            time.sleep(0.1)
        assert pidfile.exists(), "the stub CLI never started"
        pid = int(pidfile.read_text(encoding="utf-8").strip())
        cancelled = client.delete(f"/jobs/{job_id}")
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled", cancelled.text
    finally:
        for name in ("RYU_MINERU_CLI", "RYU_SMOKE_SHIM_SLEEP", "RYU_SMOKE_PIDFILE"):
            os.environ.pop(name, None)
    assert _pid_is_gone(pid), f"cancelled job left child {pid} running"
    # The job stays terminal and never flips back to succeeded behind the caller.
    assert client.get(f"/jobs/{job_id}").json()["status"] == "cancelled"
    print(f"cancel: child pid {pid} killed by DELETE /jobs/{{id}}")


def test_output_budget_is_shared_not_per_field() -> None:
    """`markdown` + `text` together must fit the budget, not each on their own.

    The 8 MiB cap exists to stay under the ext-proxy's 10 MiB body limit; two
    fields clipped to 8 MiB each would put 16 MiB on the wire and the whole
    result would be refused rather than truncated.
    """
    from ryu_mineru import parser as parser_mod

    fixture = _ROOT / "bulky.pdf"
    fixture.write_bytes(_tiny_pdf())
    ceiling = 100_000
    original = (parser_mod.MAX_OUTPUT_BYTES, parser_mod.MIN_TEXT_BUDGET_BYTES)
    os.environ["RYU_MINERU_CLI"] = str(SHIM)
    os.environ["RYU_SMOKE_SHIM_BYTES"] = "400000"
    parser_mod.MAX_OUTPUT_BYTES = ceiling
    parser_mod.MIN_TEXT_BUDGET_BYTES = 1000
    try:
        result = parser_mod.parse_file(fixture, {}, ProcessRunner(time.time() + 60))
    finally:
        parser_mod.MAX_OUTPUT_BYTES, parser_mod.MIN_TEXT_BUDGET_BYTES = original
        os.environ.pop("RYU_MINERU_CLI", None)
        os.environ.pop("RYU_SMOKE_SHIM_BYTES", None)

    md_bytes = len(result["markdown"].encode("utf-8"))
    text_bytes = len(result["text"].encode("utf-8"))
    assert md_bytes <= ceiling, md_bytes
    assert md_bytes + text_bytes <= ceiling, (md_bytes, text_bytes)
    assert result["truncated"] is True, result["truncated"]
    assert any("plain-text mirror" in w for w in result["warnings"]), result["warnings"]
    print(f"budget: markdown {md_bytes} + text {text_bytes} bytes fits one {ceiling}-byte cap")


def test_path_confinement() -> None:
    outside = _SCRATCH / "outside.pdf"
    outside.write_bytes(_tiny_pdf())

    rejected = client.post("/parse", json={"path": str(outside)})
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["error_code"] == "input_rejected", rejected.text

    traversal = client.post("/parse", json={"path": f"{_ROOT}/../outside.pdf"})
    assert traversal.status_code == 400, traversal.text

    link = _ROOT / "link.pdf"
    if not link.exists():
        link.symlink_to(outside)
    escaped = client.post("/parse", json={"path": str(link)})
    assert escaped.status_code == 400, escaped.text

    neither = client.post("/parse", json={})
    assert neither.status_code == 400, neither.text
    print("confinement: outside-root, `..`, and escaping symlink all rejected")


def test_unsupported_format_is_refused_out_loud() -> None:
    fixture = _ROOT / "notes.xyz"
    fixture.write_text("not something MinerU reads", encoding="utf-8")
    os.environ["RYU_MINERU_CLI"] = str(SHIM)
    try:
        submitted = client.post("/parse", json={"path": str(fixture)})
        assert submitted.status_code == 202, submitted.text
        snap = _await_terminal(submitted.json()["job_id"])
    finally:
        os.environ.pop("RYU_MINERU_CLI", None)
    assert snap["status"] == "failed", snap
    assert snap["error_code"] == "unsupported_format", snap
    print("format: an unreadable extension fails loudly, never as an empty document")


def test_archive_traversal_rejected() -> None:
    bomb = _ROOT / "evil.zip"
    with zipfile.ZipFile(bomb, "w") as zf:
        zf.writestr("../escaped.txt", "pwned")
    try:
        safe_extract(bomb, _SCRATCH / "extract")
        raise AssertionError("traversal member was extracted")
    except InputError as exc:
        assert "parent-directory" in str(exc), exc

    absolute = _ROOT / "absolute.zip"
    with zipfile.ZipFile(absolute, "w") as zf:
        zf.writestr("/etc/passwd", "pwned")
    try:
        safe_extract(absolute, _SCRATCH / "extract2")
        raise AssertionError("absolute member was extracted")
    except InputError as exc:
        assert "absolute" in str(exc), exc
    print("archive: `..` and absolute members rejected")


def test_markdown_shaping() -> None:
    plain = to_plain_text(
        "# Heading\n\nSome **bold** text with a [link](http://x) and "
        "<table><tr><td>EU</td></tr></table>.\n\n- first\n- second\n"
    )
    assert "Heading" in plain and "#" not in plain, plain
    assert "**" not in plain and "link" in plain, plain
    assert "EU" in plain and "<table>" not in plain, plain

    clipped, whole = truncate("x" * 100, 10)
    assert whole is False and len(clipped) == 10, (clipped, whole)
    kept, whole = truncate("short", 100)
    assert whole is True and kept == "short"
    print("markdown: plain-text variant strips markup, truncation is flagged not dropped")


def test_real_mineru_parse(available: bool) -> None:
    """Opt-in: the real binary over a real PDF. Downloads models on first run."""
    if not (available and os.environ.get("RYU_MINERU_SMOKE_REAL") == "1"):
        return
    fixture = _ROOT / "real.pdf"
    fixture.write_bytes(_tiny_pdf())
    submitted = client.post("/parse", json={"path": str(fixture)})
    assert submitted.status_code == 202, submitted.text
    snap = _await_terminal(submitted.json()["job_id"], timeout=1800)
    assert snap["status"] == "succeeded", snap
    print(f"real: MinerU parsed the fixture -> {len(snap['result']['markdown'])} md chars")


def main() -> None:
    test_auth_is_fail_closed()
    cap = test_capability_answers_without_the_library()
    available = bool(cap["available"])
    test_parse_without_a_cli(available)
    test_parse_roundtrip_against_the_stub()
    test_inline_parse_accepted()
    test_timeout_kills_the_child()
    test_cancel_kills_the_child()
    test_output_budget_is_shared_not_per_field()
    test_path_confinement()
    test_unsupported_format_is_refused_out_loud()
    test_archive_traversal_rejected()
    test_markdown_shaping()
    test_real_mineru_parse(available)
    assert STORE.list(), "the job table lost every job it was handed"
    mode = "with the mineru CLI installed" if available else "without the mineru CLI"
    real = " + a real parse" if os.environ.get("RYU_MINERU_SMOKE_REAL") == "1" and available else ""
    print(f"\nSMOKE_OK ({mode}, parse path exercised via the CLI stub{real})")


if __name__ == "__main__":
    main()
