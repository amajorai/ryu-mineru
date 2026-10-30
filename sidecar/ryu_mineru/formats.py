"""What MinerU can read, and what it can read *here*.

The format list is not static, and pretending it is would be the "plausible-
looking lie" this whole capability exists to kill. MinerU parses PDFs and page
images natively. Office documents are the moving part:

* **MinerU 3.x reads OOXML directly** — `mineru/backend/office/` walks the
  package itself, no external converter involved.
* **MinerU 2.x did not**, and needed LibreOffice to turn the document into a PDF
  first.

So a `.docx` is genuinely unreadable on a 2.x host without `soffice`, and must
not appear in the composer's `accept` list there. The legacy *binary* formats
(`.doc`, `.ppt`, `.xls`) are absent from this list on purpose: MinerU never read
them. `unstructured` is the backend for a corpus full of those.
"""

from __future__ import annotations

from .cli import mineru_major
from .deps import LIBREOFFICE

# Parsed directly by MinerU's own layout/OCR pipeline.
CORE_EXTENSIONS: frozenset[str] = frozenset(
    {".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
)

# OOXML. Native on MinerU 3.x; LibreOffice-converted on 2.x.
OFFICE_EXTENSIONS: frozenset[str] = frozenset({".docx", ".pptx", ".xlsx"})

# Expanded and parsed member by member (see `paths.safe_extract`).
ARCHIVE_EXTENSIONS: frozenset[str] = frozenset(
    {".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz"}
)

# How (or whether) this host reads Office documents.
NATIVE = "native"
VIA_LIBREOFFICE = "libreoffice"
UNAVAILABLE = "unavailable"
UNKNOWN = "unknown"


def office_support() -> str:
    """One of NATIVE / VIA_LIBREOFFICE / UNAVAILABLE / UNKNOWN.

    UNKNOWN means MinerU is not installed at all, in which case the honest answer
    to "can you read this" is `available: false`, not a narrowed format list.
    """
    major = mineru_major()
    if major is None:
        return UNKNOWN
    if major >= 3:
        return NATIVE
    return VIA_LIBREOFFICE if LIBREOFFICE.present() else UNAVAILABLE


def supported_extensions() -> list[str]:
    """Lowercase, dot-prefixed, sorted — the `/capability` `formats` list."""
    usable = set(CORE_EXTENSIONS) | set(ARCHIVE_EXTENSIONS)
    if office_support() != UNAVAILABLE:
        usable |= set(OFFICE_EXTENSIONS)
    return sorted(usable)


def missing_dependencies() -> list[str]:
    """Native tools absent on this host, by name — the contract's flat list.

    Empty on a 3.x install, which needs nothing outside pip. Reporting a missing
    LibreOffice there would be noise that sends users to `brew` for no reason.
    """
    return [LIBREOFFICE.key] if office_support() == UNAVAILABLE else []
