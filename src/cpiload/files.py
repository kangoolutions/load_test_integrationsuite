"""Ordner-Browser und Datei-Scan unterhalb des gemounteten Datenverzeichnisses.

Alles hier ist blockierend (Dateisystem) und wird aus async-Code per
`asyncio.to_thread` aufgerufen. Ausgelegt auf Ordner mit >100k Dateien.

Über Docker-Desktop-Bind-Mounts (macOS/Windows) dauert allein das Auflisten
von 280k Einträgen ~40 s, nativ < 1 s. Deshalb cached `DirCache` die Listings
pro Ordner und validiert sie über die mtime des Ordners (ändert sich, sobald
Dateien hinzukommen oder verschwinden). Browser, Vorschau und Start eines
Laufs teilen sich so einen einzigen Scan.
"""

from __future__ import annotations

import fnmatch
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

_NATURAL_SPLIT = re.compile(r"(\d+)")


class PathError(ValueError):
    """Pfad liegt außerhalb des Datenverzeichnisses oder existiert nicht."""


def resolve_in_base(base: Path, rel: str) -> Path:
    """Löst `rel` relativ zu `base` auf und verhindert Ausbrüche per `..`/Symlink."""
    base = base.resolve()
    target = (base / rel.strip("/")).resolve()
    if target != base and base not in target.parents:
        raise PathError("Pfad liegt außerhalb des Datenverzeichnisses.")
    if not target.is_dir():
        raise PathError(f"Ordner nicht gefunden: /{rel.strip('/')}")
    return target


def natural_key(name: str) -> list:
    """`msg_2.xml` vor `msg_10.xml`."""
    return [int(p) if p.isdigit() else p.lower() for p in _NATURAL_SPLIT.split(name)]


@dataclass(frozen=True, slots=True)
class Listing:
    mtime_ns: int
    dirs: tuple[str, ...]   # natürlich sortiert, ohne versteckte Einträge
    files: tuple[str, ...]  # natürlich sortiert, ohne versteckte Einträge


def _read_dir(directory: Path, progress: dict[str, int] | None = None, key: str = "") -> Listing:
    mtime = directory.stat().st_mtime_ns
    dirs: list[str] = []
    files: list[str] = []
    seen = 0
    try:
        with os.scandir(directory) as it:
            for entry in it:
                seen += 1
                if progress is not None and seen % 5000 == 0:
                    progress[key] = seen
                if entry.name.startswith("."):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    dirs.append(entry.name)
                elif entry.is_file():
                    files.append(entry.name)
    finally:
        if progress is not None:
            progress.pop(key, None)
    dirs.sort(key=natural_key)
    files.sort(key=natural_key)
    return Listing(mtime, tuple(dirs), tuple(files))


class DirCache:
    """Thread-sicherer LRU-Cache für Ordner-Listings mit Scan-Fortschritt."""

    def __init__(self, base: Path, max_entries: int = 32) -> None:
        self._base = base.resolve()
        self._max = max_entries
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, Listing] = OrderedDict()
        self._path_locks: dict[str, threading.Lock] = {}
        self._progress: dict[str, int] = {}

    def listing(self, directory: Path, refresh: bool = False) -> Listing:
        key = str(directory)
        with self._lock:
            path_lock = self._path_locks.setdefault(key, threading.Lock())
        # Parallele Anfragen für denselben Ordner warten auf den ersten Scan.
        with path_lock:
            mtime = directory.stat().st_mtime_ns
            with self._lock:
                hit = self._entries.get(key)
                if hit is not None and hit.mtime_ns == mtime and not refresh:
                    self._entries.move_to_end(key)
                    return hit
            rel = directory.relative_to(self._base).as_posix() if directory != self._base else ""
            listing = _read_dir(directory, self._progress, rel)
            with self._lock:
                self._entries[key] = listing
                self._entries.move_to_end(key)
                while len(self._entries) > self._max:
                    self._entries.popitem(last=False)
            return listing

    def progress(self) -> list[dict]:
        """Laufende Scans: Ordner (relativ) und bisher gelesene Einträge."""
        return [{"path": path, "entries": n} for path, n in list(self._progress.items())]


def _listing(directory: Path, cache: DirCache | None, refresh: bool = False) -> Listing:
    return cache.listing(directory, refresh) if cache is not None else _read_dir(directory)


def _rel(base: Path, path: Path) -> str:
    base = base.resolve()
    return "" if path == base else path.relative_to(base).as_posix()


def _patterns(pattern: str) -> list[str]:
    parts = [p.strip().lower() for p in re.split(r"[,;]", pattern or "") if p.strip()]
    return parts or ["*"]


def _filter(names: tuple[str, ...], patterns: list[str]) -> list[str]:
    if patterns == ["*"]:
        return list(names)
    return [n for n in names if any(fnmatch.fnmatchcase(n.lower(), p) for p in patterns)]


def list_folder(base: Path, rel: str = "", cache: DirCache | None = None, refresh: bool = False) -> dict:
    """Unterordner + Anzahl Dateien (nicht rekursiv) für den Ordner-Browser."""
    target = resolve_in_base(base, rel)
    listing = _listing(target, cache, refresh)
    path = _rel(base, target)
    return {
        "path": path,
        "parent": None if not path else path.rpartition("/")[0],
        "dirs": [{"name": d, "path": f"{path}/{d}" if path else d} for d in listing.dirs],
        "files": len(listing.files),
    }


def scan_files(
    base: Path,
    rel: str,
    pattern: str = "*",
    recursive: bool = False,
    limit: int = 0,
    cache: DirCache | None = None,
) -> list[str]:
    """Liefert passende Dateien als Pfade relativ zu `base`, natürlich sortiert.

    Sortiert wird vor dem Limit, damit ein Smoke-Test mit N Dateien immer
    dieselben ersten N Dateien trifft.
    """
    root = resolve_in_base(base, rel)
    patterns = _patterns(pattern)
    root_rel = _rel(base, root)
    prefix = f"{root_rel}/" if root_rel else ""

    if not recursive:
        found = [prefix + n for n in _filter(_listing(root, cache).files, patterns)]
    else:
        found = []
        stack = [(root, prefix)]
        while stack:
            current, current_prefix = stack.pop()
            listing = _listing(current, cache)
            found.extend(current_prefix + n for n in _filter(listing.files, patterns))
            stack.extend((current / d, f"{current_prefix}{d}/") for d in listing.dirs)
        found.sort(key=natural_key)

    if limit and limit > 0:
        found = found[:limit]
    return found


def preview(base: Path, rel: str, pattern: str, recursive: bool, limit: int, cache: DirCache | None = None) -> dict:
    files = scan_files(base, rel, pattern, recursive, 0, cache)
    total = len(files)
    effective = min(total, limit) if limit and limit > 0 else total
    return {"matches": total, "effective": effective, "sample": files[:8]}
