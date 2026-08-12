"""Path confinement and safe archive expansion.

Two separate jobs, both fail-closed:

1. `resolve_input` — the parse request names a file by *path* (Core hands over a
   content-addressed blob under `${RYU_DIR}/blobs/…`, never an upload), so the
   path is attacker-influenced input. It is resolved through symlinks and then
   required to live under an allow-listed root. Without the post-resolution
   containment check, a symlink planted inside the blob dir reads `/etc/shadow`
   and returns it as "document text".

2. `safe_extract` — an archive's member names are attacker-controlled strings.
   Absolute names, `..` segments, and symlink/hardlink/device members are all
   rejected outright rather than sanitised, because a rewritten name is a guess
   at intent and a refusal is not.

This mirrors `apps-store/unstructured/sidecar/ryu_unstructured/paths.py`
deliberately: four `document.parse` backends sharing one containment story is
worth more than four subtly different ones.
"""

from __future__ import annotations

import os
import tarfile
import tempfile
import zipfile
from pathlib import Path

from .limits import MAX_ARCHIVE_BYTES, MAX_ARCHIVE_MEMBERS, MAX_INPUT_BYTES


class InputError(ValueError):
    """A caller-supplied path or archive that we refuse to open."""


def workdir() -> Path:
    """Scratch root this process owns: staged uploads, CLI output trees, models.

    Core points `RYU_MINERU_WORKDIR` at `${RYU_DIR}/cache/mineru` so the model
    downloads and the multi-megabyte output trees land on the same volume as the
    rest of the node's data, not in `/tmp` where a reboot (or a full tmpfs) eats
    a 20 GB model set.
    """
    configured = (os.environ.get("RYU_MINERU_WORKDIR") or "").strip()
    root = (
        Path(configured).expanduser()
        if configured
        else Path(tempfile.gettempdir()) / "ryu-mineru"
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


def allowed_roots() -> list[Path]:
    """Directories a parse input may live under.

    `RYU_MINERU_ROOTS` is a `os.pathsep`-separated list Core sets from the
    manifest (`${RYU_DIR}` is the only token the manifest may interpolate). With
    nothing set we fall back to Core's own injected `RYU_DIR`, and only then to
    `~/.ryu` — that last one is NOT profile-aware, so bottoming out there under
    the dev profile (`~/.ryu-dev`) would reject every blob parse. Order matters.
    An empty allow-list must never mean "everything".
    """
    raw = (os.environ.get("RYU_MINERU_ROOTS") or "").strip()
    if not raw:
        raw = os.environ.get("RYU_DIR") or str(Path.home() / ".ryu")
    roots: list[Path] = []
    for part in raw.split(os.pathsep):
        candidate = part.strip()
        if not candidate:
            continue
        try:
            roots.append(Path(candidate).expanduser().resolve())
        except OSError:
            continue
    return roots


def _is_within(child: Path, parent: Path) -> bool:
    """True when `child` is `parent` or sits beneath it.

    `Path.is_relative_to` is 3.9+, and both sides are already fully resolved, so
    this is a pure lexical comparison over real paths.
    """
    return child == parent or parent in child.parents


def resolve_input(raw_path: str) -> Path:
    """Resolve a requested input path, or raise `InputError`.

    Symlinks are followed *before* the containment test on purpose: the question
    is where the bytes actually live, not what the name looks like.
    """
    candidate = (raw_path or "").strip()
    if not candidate:
        raise InputError("missing `path`")
    try:
        resolved = Path(candidate).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InputError(f"cannot open `{candidate}`: {exc}") from exc
    if not resolved.is_file():
        raise InputError(f"`{candidate}` is not a regular file")

    roots = allowed_roots()
    if not any(_is_within(resolved, root) for root in roots):
        readable = ", ".join(str(root) for root in roots) or "(none)"
        raise InputError(
            f"`{candidate}` resolves outside the allowed roots ({readable}); "
            "set RYU_MINERU_ROOTS to widen them"
        )

    size = resolved.stat().st_size
    if size > MAX_INPUT_BYTES:
        raise InputError(
            f"input is {size} bytes, over the {MAX_INPUT_BYTES}-byte limit "
            "(raise RYU_MINERU_MAX_INPUT_BYTES to allow it)"
        )
    return resolved


ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")


def is_archive(path: Path) -> bool:
    """Whether we should expand this input and parse its members."""
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in ARCHIVE_SUFFIXES)


def _check_member_name(name: str) -> str:
    """Reject a member name that escapes, or return its normalised relative form."""
    if not name or name in (".", "./"):
        raise InputError("archive member with an empty name")
    if name.startswith("/") or name.startswith("\\"):
        raise InputError(f"archive member `{name}` is an absolute path")
    pure = Path(name.replace("\\", "/"))
    if pure.is_absolute() or pure.drive or pure.root:
        raise InputError(f"archive member `{name}` is an absolute path")
    if any(part == ".." for part in pure.parts):
        raise InputError(f"archive member `{name}` contains a parent-directory reference")
    return str(pure)


def _finalise(dest_root: Path, relative: str) -> Path:
    """Belt-and-braces containment check on the concrete destination path."""
    target = (dest_root / relative).resolve()
    if not _is_within(target, dest_root):
        raise InputError(f"archive member `{relative}` escapes the extraction directory")
    return target


def safe_extract(archive: Path, dest_root: Path) -> list[Path]:
    """Expand `archive` into `dest_root`, returning the extracted regular files.

    Directories are created as needed; every other member kind (symlink,
    hardlink, fifo, device) is refused, since none of them can carry document
    bytes and all of them can redirect a later write.
    """
    dest_root = dest_root.resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        return _extract_zip(archive, dest_root)
    try:
        if tarfile.is_tarfile(archive):
            return _extract_tar(archive, dest_root)
    except (OSError, tarfile.TarError) as exc:
        raise InputError(f"unreadable archive: {exc}") from exc
    raise InputError(f"`{archive.name}` is not a readable zip or tar archive")


def _extract_zip(archive: Path, dest_root: Path) -> list[Path]:
    written: list[Path] = []
    total = 0
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            raise InputError(
                f"archive has {len(infos)} members, over the {MAX_ARCHIVE_MEMBERS} limit"
            )
        for info in infos:
            relative = _check_member_name(info.filename)
            target = _finalise(dest_root, relative)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            # The high 16 bits of external_attr are the unix mode; S_IFLNK there
            # is a symlink member, which `ZipFile.extract` would write as a file
            # containing the link target and some tools would then follow.
            mode = info.external_attr >> 16
            if mode and (mode & 0o170000) == 0o120000:
                raise InputError(f"archive member `{info.filename}` is a symlink")
            total += info.file_size
            if total > MAX_ARCHIVE_BYTES:
                raise InputError(f"archive expands past the {MAX_ARCHIVE_BYTES}-byte limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                _copy_bounded(src, dst, MAX_ARCHIVE_BYTES)
            written.append(target)
    return written


def _extract_tar(archive: Path, dest_root: Path) -> list[Path]:
    written: list[Path] = []
    total = 0
    with tarfile.open(archive) as tf:
        count = 0
        for member in tf:
            count += 1
            if count > MAX_ARCHIVE_MEMBERS:
                raise InputError(f"archive has over {MAX_ARCHIVE_MEMBERS} members")
            relative = _check_member_name(member.name)
            target = _finalise(dest_root, relative)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if member.issym() or member.islnk():
                raise InputError(f"archive member `{member.name}` is a link")
            if not member.isfile():
                raise InputError(f"archive member `{member.name}` is not a regular file")
            total += member.size
            if total > MAX_ARCHIVE_BYTES:
                raise InputError(f"archive expands past the {MAX_ARCHIVE_BYTES}-byte limit")
            src = tf.extractfile(member)
            if src is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with src, target.open("wb") as dst:
                _copy_bounded(src, dst, MAX_ARCHIVE_BYTES)
            written.append(target)
    return written


def _copy_bounded(src, dst, ceiling: int) -> None:
    """Stream member bytes, stopping if the declared size was a lie."""
    remaining = ceiling
    while True:
        chunk = src.read(64 * 1024)
        if not chunk:
            return
        remaining -= len(chunk)
        if remaining < 0:
            raise InputError("archive member is larger than its declared size")
        dst.write(chunk)
