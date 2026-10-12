"""Finding a `csv_dir` source's files (spec 026), confined to the server's file sources folder.

The folder is the source's `path` under `TABAYYUN_FILE_SOURCES_ROOT`, resolved; it must stay
inside the root. Every file is resolved too, so a symbolic link cannot lead out of the root.
Error messages name paths relative to the root, never the server's own layout above it.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from tabayyun.connectors.csv_dir.config import CsvDirConfig
from tabayyun.connectors.errors import ConnectorError

SECOND_NS = 1_000_000_000
EXTENSIONS = (".csv", ".parquet")
TEMP_SUFFIXES = (".tmp", ".part", ".partial", ".crdownload")
MAX_ENTRIES = 100_000
HASH_CHUNK = 1024 * 1024
NOT_ENABLED = "file sources are not enabled on this server (TABAYYUN_FILE_SOURCES_ROOT)"


@dataclass(frozen=True)
class FoundFile:
    """A file to consider: its path relative to the source's folder, where it is, size and time."""

    path: str
    full: Path
    size: int
    mtime_ns: int


@dataclass
class Listing:
    """What a listing found: the files ready to read, oldest first, and what it skipped."""

    files: list[FoundFile] = field(default_factory=list)
    matched: int = 0
    bytes: int = 0
    settling: int = 0
    too_large: list[str] = field(default_factory=list)
    outside_root: list[str] = field(default_factory=list)
    truncated: bool = False


def _refuse(message: str) -> ConnectorError:
    return ConnectorError(message, retryable=False)


def folder(root: Path | None, relative: str) -> Path:
    """The source's folder, resolved inside `root`.

    Raises:
        ConnectorError: the root is unset or missing, or the folder is outside it, missing, not
            a folder or unreadable (not retryable: someone has to fix the folder or the config).
    """
    if root is None:
        raise _refuse(NOT_ENABLED)
    if not root.is_dir():
        raise _refuse("the file sources folder of this server does not exist")
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise _refuse(f"folder {relative} is outside the file sources folder")
    if not target.exists():
        raise _refuse(f"folder {relative} does not exist")
    if not target.is_dir():
        raise _refuse(f"{relative} is not a folder")
    if not os.access(target, os.R_OK | os.X_OK):
        raise _refuse(f"folder {relative} cannot be read")
    return target


def skipped_name(name: str) -> bool:
    """Hidden and temporary names: being written or not meant to be read."""
    lower = name.lower()
    return name.startswith((".", "~$")) or name.endswith("~") or lower.endswith(TEMP_SUFFIXES)


def wanted(name: str, pattern: str) -> bool:
    """A `.csv` or `.parquet` file whose name matches the pattern (case-sensitive)."""
    return name.lower().endswith(EXTENSIONS) and fnmatch.fnmatchcase(name, pattern)


def _entries(base: Path, recursive: bool) -> list[tuple[Path, str]]:
    """(directory, file name) of every non-hidden entry, at most `MAX_ENTRIES` plus one."""
    out: list[tuple[Path, str]] = []
    if not recursive:
        with os.scandir(base) as it:
            for entry in it:
                if entry.is_file() or entry.is_symlink():
                    out.append((base, entry.name))
                if len(out) > MAX_ENTRIES:
                    break
        return out
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        out.extend((Path(dirpath), name) for name in filenames)
        if len(out) > MAX_ENTRIES:
            break
    return out


def list_files(root: Path, base: Path, config: CsvDirConfig, now_ns: int) -> Listing:
    """The files of `base` (inside `root`) a scan may read, oldest first, and what it skipped.

    Settling files (changed within `settle_s`), files over `max_file_mb` and links out of the
    root are skipped; at most `MAX_ENTRIES` entries are looked at.
    """
    listing = Listing()
    entries = _entries(base, config.recursive)
    if len(entries) > MAX_ENTRIES:
        listing.truncated = True
        entries = entries[:MAX_ENTRIES]
    settled_before = now_ns - config.settle_s * SECOND_NS
    for directory, name in entries:
        if skipped_name(name) or not wanted(name, config.pattern):
            continue
        full = directory / name
        relative = full.relative_to(base).as_posix()
        if any(part.startswith(".") for part in Path(relative).parts[:-1]):
            continue
        resolved = full.resolve()
        if not resolved.is_relative_to(root):
            listing.outside_root.append(relative)
            continue
        try:
            stat = resolved.stat()
        except OSError:
            continue  # gone since the listing
        if not resolved.is_file():
            continue
        listing.matched += 1
        listing.bytes += stat.st_size
        if stat.st_size > config.max_file_bytes:
            listing.too_large.append(relative)
            continue
        if stat.st_mtime_ns > settled_before:
            listing.settling += 1
            continue
        listing.files.append(FoundFile(relative, resolved, stat.st_size, stat.st_mtime_ns))
    listing.files.sort(key=lambda f: (f.mtime_ns, f.path))
    return listing


def sha256(path: Path) -> str:
    """Hex SHA-256 of a file's content; blocking."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
