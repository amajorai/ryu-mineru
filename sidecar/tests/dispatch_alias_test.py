"""Blob-path dispatch regressions.

Core's in-process path hands over `${RYU_DIR}/blobs/ab/<sha256>`, which has no
extension, so the caller's `filename` must decide the format. Run from the
sidecar root: `python3 -m unittest discover -s tests -p '*_test.py'`.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ryu_mineru import jobs
from ryu_mineru.paths import dispatch_suffix, is_archive, stage_dispatch_alias

SHA = "ab" * 32


class DispatchAliasTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.blob = root / "blobs" / "ab" / SHA
        self.blob.parent.mkdir(parents=True)
        self.blob.write_bytes(b"%PDF-1.7 fixture bytes")
        self.staging = root / "uploads"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_suffix_comes_from_the_original_filename(self) -> None:
        self.assertEqual(dispatch_suffix("Q3 Report.PDF"), ".pdf")
        self.assertEqual(dispatch_suffix("dir\\deck.pptx"), ".pptx")
        self.assertEqual(dispatch_suffix("bundle.tar.gz"), ".tar.gz")
        self.assertEqual(dispatch_suffix("notes"), "")
        self.assertEqual(dispatch_suffix(None), "")
        self.assertEqual(dispatch_suffix("x." + "a" * 40), "")

    def test_an_extensionless_blob_gets_an_alias_carrying_the_extension(self) -> None:
        alias = stage_dispatch_alias(self.blob, "paper.pdf", self.staging)
        self.assertIsNotNone(alias)
        assert alias is not None
        self.assertEqual(alias.suffix, ".pdf")
        self.assertEqual(alias.parent, self.staging)
        self.assertEqual(alias.read_bytes(), self.blob.read_bytes())
        alias.unlink()
        self.assertTrue(self.blob.exists(), "Core's blob must never be removed")

    def test_compound_archive_suffix_still_reads_as_an_archive(self) -> None:
        alias = stage_dispatch_alias(self.blob, "corpus.tar.gz", self.staging)
        assert alias is not None
        self.assertTrue(is_archive(alias))

    def test_no_alias_when_nothing_would_change(self) -> None:
        self.assertIsNone(stage_dispatch_alias(self.blob, None, self.staging))
        self.assertIsNone(stage_dispatch_alias(self.blob, "README", self.staging))
        named = self.blob.with_name("already.pdf")
        named.write_bytes(b"x")
        self.assertIsNone(stage_dispatch_alias(named, "other.pdf", self.staging))

    def test_job_reports_the_callers_filename_not_the_on_disk_name(self) -> None:
        store = jobs.JobStore()
        with patch.object(jobs, "parse_file", side_effect=lambda *_a, **_k: {"markdown": "ok"}):
            job = store.submit(self.blob, {}, display_name="paper.pdf")
            deadline = time.monotonic() + 3
            while job.status not in jobs.TERMINAL and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(job.snapshot()["filename"], "paper.pdf")


if __name__ == "__main__":
    unittest.main()
