"""System-dependency and hardware detection.

MinerU is the heaviest of the four `document.parse` backends, and its two ways of
disappointing a user are both silent by default:

1. **A missing native converter.** `.docx` reaches MinerU only after LibreOffice
   has turned it into a PDF. Without `soffice` that is a stack trace from inside
   a conversion helper, which reaches the user as "this document has no text".
2. **Not enough machine.** The pipeline backend wants 16 GB of RAM and the VLM
   backends want 8 GB of VRAM. A 8 GB laptop does not fail fast — it swaps, then
   the OOM killer takes the child, and the job reports an opaque non-zero exit.

So both are probed and reported up front through `/capability`, where a user
deciding whether to install a multi-gigabyte backend can see them.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from typing import Optional

_GIB = 1024 * 1024 * 1024

# Upstream's stated floors. Quoted here so the numbers this sidecar reports and
# the numbers the README promises cannot drift apart.
MIN_RAM_BYTES = 16 * _GIB
RECOMMENDED_RAM_BYTES = 32 * _GIB
MIN_VRAM_PIPELINE_BYTES = 4 * _GIB
MIN_VRAM_VLM_BYTES = 8 * _GIB
MIN_DISK_BYTES = 20 * _GIB


@dataclass(frozen=True)
class SystemDep:
    """One native dependency: how to detect it and how to install it."""

    key: str
    # Any one of these on PATH satisfies the dependency (libreoffice ships as
    # `soffice` on macOS and as `libreoffice` on most Linux distros).
    binaries: tuple[str, ...]
    purpose: str
    brew: str
    apt: str

    def present(self) -> bool:
        return any(shutil.which(binary) for binary in self.binaries)

    def describe(self) -> dict[str, object]:
        return {
            "key": self.key,
            "present": self.present(),
            "purpose": self.purpose,
            "install": {"brew": self.brew, "apt": self.apt},
        }

    def message(self) -> str:
        return (
            f"{self.key} is not installed — {self.purpose}. "
            f"Install it with `{self.brew}` (macOS) or `{self.apt}` (Debian/Ubuntu)."
        )


LIBREOFFICE = SystemDep(
    key="libreoffice",
    binaries=("soffice", "libreoffice"),
    purpose=(
        "converting OOXML Office documents to PDF before parsing, on MinerU 2.x. "
        "MinerU 3.x reads them itself and needs nothing here"
    ),
    brew="brew install --cask libreoffice",
    apt="apt-get install -y libreoffice",
)

# The whole native-tool surface, and it is one entry that only 2.x needs. This is
# MinerU's genuine advantage over `unstructured`, which shells out to four
# binaries none of which pip can install: MinerU's weight is all inside the venv.
ALL_DEPS: tuple[SystemDep, ...] = (LIBREOFFICE,)


def total_ram_bytes() -> Optional[int]:
    """Physical RAM, or None where the platform will not say.

    Deliberately stdlib-only: adding `psutil` to a sidecar whose whole pitch is
    "it is already the heaviest install" would be poor taste.
    """
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return None
    if pages <= 0 or page_size <= 0:
        return None
    return int(pages) * int(page_size)


def free_disk_bytes(path: str) -> Optional[int]:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def hardware(workdir: str) -> dict[str, object]:
    """What this host has, against what MinerU asks for.

    `meets_minimum` is advisory — a parse is never refused on it. An underpowered
    host succeeding slowly is a better outcome than a gate that guesses wrong,
    and a host that genuinely cannot cope reports it through the job's error.
    """
    ram = total_ram_bytes()
    disk = free_disk_bytes(workdir)
    return {
        "platform": sys.platform,
        "total_ram_bytes": ram,
        "free_disk_bytes": disk,
        "min_ram_bytes": MIN_RAM_BYTES,
        "recommended_ram_bytes": RECOMMENDED_RAM_BYTES,
        "min_disk_bytes": MIN_DISK_BYTES,
        "min_vram_bytes": {
            "pipeline": MIN_VRAM_PIPELINE_BYTES,
            "vlm": MIN_VRAM_VLM_BYTES,
        },
        "meets_minimum_ram": None if ram is None else ram >= MIN_RAM_BYTES,
        "meets_minimum_disk": None if disk is None else disk >= MIN_DISK_BYTES,
    }


def hardware_warnings(workdir: str) -> list[str]:
    """Non-fatal degradations worth stapling to a job result."""
    notes: list[str] = []
    ram = total_ram_bytes()
    if ram is not None and ram < MIN_RAM_BYTES:
        notes.append(
            f"this host has {ram / _GIB:.1f} GB of RAM; MinerU's stated minimum is "
            f"{MIN_RAM_BYTES // _GIB} GB (32 GB recommended). Large documents may be "
            "killed by the OS before they finish."
        )
    disk = free_disk_bytes(workdir)
    if disk is not None and disk < MIN_DISK_BYTES:
        notes.append(
            f"{disk / _GIB:.1f} GB free where models are cached; MinerU's model set "
            f"wants about {MIN_DISK_BYTES // _GIB} GB on first run."
        )
    return notes
