"""Filesystem operations, kept honest about the edges.

The existing ``file`` node reads and writes one text file inside the project
root. That covers "stash this string" and nothing an RPA actually needs: an
automation that downloads a folder of invoices, renames them, and moves the
sorted pile somewhere has no way to express any of it.

Three decisions are load-bearing and each is a place where the obvious
implementation is wrong:

**Paths are resolved, not string-joined.** ``PROJECT_ROOT / "../../.ssh"``
resolves outside the root, and a flow triggered by a webhook carries whatever
path the caller sent. Every entry point goes through :func:`resolve_under_root`,
which resolves symlinks and then checks containment — string-prefix matching on
the *unresolved* path is the classic bypass, because ``/proj/../etc`` and
``/etc`` share nothing but a prefix of the wrong kind.

**A symlink is a decision, not a detail.** ``root/link -> /etc`` resolves
outside the root, and by the time the caller has written through it the damage
is done. A path that escapes only *after* resolution is refused with the
resolved location in the message, because "路径越出项目根目录" alone leaves the
author guessing which of three plausible paths was the problem.

**Nothing is deleted by a wildcard.** ``delete`` requires the path to name
something specific and ``delete_dir`` is recursive-only when asked for
explicitly. A glob that matched the project root because a variable expanded to
``*`` is not a bug report, it is a catastrophe, and the guard is cheap.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

#: Extensions treated as text by the read/write helpers. Reading a 40 MB binary
#: as UTF-8 produces replacement characters rather than an error, which is the
#: worst outcome: a flow that "worked" on data that was never read.
TEXT_SUFFIXES = frozenset({
    ".txt", ".md", ".json", ".jsonl", ".csv", ".tsv", ".log", ".yaml", ".yml",
    ".ini", ".cfg", ".toml", ".xml", ".html", ".css", ".js", ".ts", ".py",
    ".sh", ".sql", ".env", ".conf",
})

#: Refuse to load a file larger than this in one go. 20k characters is already
#: far past what a prompt or a single log line should carry.
MAX_TEXT_BYTES = 2_000_000

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff"})


class PathRefused(ValueError):
    """The path resolves outside the root it must stay inside."""


def resolve_under_root(root: Path, raw: str | os.PathLike[str]) -> Path:
    """Resolve ``raw`` and refuse anything landing outside ``root``.

    ``strict=False`` so a path that does not exist yet still resolves — the
    write path depends on that. The containment check is on the *resolved* path
    because that is the only one the filesystem will honour.
    """
    if raw is None or str(raw).strip() == "":
        raise PathRefused("路径为空")
    root_real = Path(root).resolve()
    candidate = Path(root_real) / str(raw)
    resolved = candidate.resolve(strict=False)
    if resolved != root_real and root_real not in resolved.parents:
        raise PathRefused(
            f"路径 {str(raw)!r} 解析后落在项目根目录之外（{resolved}），已拒绝"
        )
    return resolved


def relative_to_root(root: Path, path: Path) -> str:
    try:
        return str(Path(path).relative_to(Path(root).resolve()))
    except ValueError:  # pragma: no cover - resolve_under_root already guarantees it
        return str(path)


def is_text_file(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES


@dataclass
class WalkOptions:
    pattern: str = "*"
    recursive: bool = True
    include_dirs: bool = False
    max_results: int = 1000
    follow_symlinks: bool = False


def walk(root: Path, opts: WalkOptions) -> list[dict[str, Any]]:
    """List what is under ``root``, newest first.

    Sorted by mtime because that is the order an operator means by "the files
    from this morning", and an unsorted listing makes a human sort it by hand
    every time. Capped, because a flow that walks into a directory with 200k
    files should not take the process down, and the cap is reported rather than
    applied silently.
    """
    base = Path(root).resolve()
    if not base.is_dir():
        raise PathRefused(f"{base} 不是目录")
    out: list[dict[str, Any]] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(base, followlinks=opts.follow_symlinks):
        current = Path(dirpath)
        if current != base and not opts.recursive:
            dirnames[:] = []
            continue
        if opts.include_dirs:
            for name in dirnames:
                if not fnmatch.fnmatch(name, opts.pattern):
                    continue
                out.append(_entry(current / name, is_dir=True))
        for name in filenames:
            if not fnmatch.fnmatch(name, opts.pattern):
                continue
            if len(out) >= opts.max_results:
                truncated = True
                break
            out.append(_entry(current / name, is_dir=False))
        if truncated:
            break
        if not opts.recursive:
            break
    out.sort(key=lambda item: item["mtime"], reverse=True)
    if truncated:
        out.append({"path": "", "name": "", "is_dir": False, "size": 0,
                    "mtime": 0.0, "truncated": True})
    return out


def _entry(path: Path, is_dir: bool) -> dict[str, Any]:
    try:
        stat = path.stat()
        size, mtime = stat.st_size, stat.st_mtime
    except OSError:
        # A file that vanished between listing and stat is normal in a live
        # directory, and reporting a zeroed entry beats failing the whole walk.
        size, mtime = 0, 0.0
    return {
        "path": str(path),
        "name": path.name,
        "is_dir": is_dir,
        "size": size,
        "mtime": mtime,
        "truncated": False,
    }


def ensure_unique(target: Path) -> Path:
    """``a.txt`` -> ``a (1).txt``. Never overwrites on a batch copy."""
    if not target.exists():
        return target
    stem, suffix, parent = target.stem, target.suffix, target.parent
    for index in range(1, 10_000):
        candidate = parent / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    raise PathRefused(f"{target} 已存在且无法找到可用文件名")  # pragma: no cover


def copy_one(src: Path, dst_dir: Path, overwrite: bool = False) -> dict[str, Any]:
    dst_dir.mkdir(parents=True, exist_ok=True)
    target = dst_dir / src.name
    if target.exists():
        if not overwrite:
            target = ensure_unique(target)
        else:
            target.unlink()
    shutil.copy2(src, target)
    return {"from": str(src), "to": str(target), "size": target.stat().st_size}


def move_one(src: Path, dst: Path, overwrite: bool = False) -> dict[str, Any]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if not overwrite:
            raise PathRefused(f"目标已存在: {dst}；请换个目标名，或开启覆盖后重试")
        dst.unlink()
    shutil.move(str(src), str(dst))
    return {"from": str(src), "to": str(dst)}


def read_text(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"read": False, "path": str(path), "content": "", "reason": "文件不存在"}
    if not path.is_file():
        return {"read": False, "path": str(path), "content": "", "reason": "不是文件"}
    if path.stat().st_size > MAX_TEXT_BYTES:
        return {
            "read": False, "path": str(path), "content": "",
            "reason": f"文件 {path.stat().st_size} 字节超过上限 {MAX_TEXT_BYTES}",
        }
    if not is_text_file(path) and path.suffix.lower() not in IMAGE_SUFFIXES:
        return {
            "read": False, "path": str(path), "content": "",
            "reason": f"扩展名 {path.suffix!r} 不在文本类型白名单里，拒绝按 UTF-8 读",
        }
    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        "read": True, "path": str(path), "content": text,
        "bytes": path.stat().st_size, "lines": text.count("\n") + (0 if not text else 1),
    }


def delete_path(path: Path, recursive: bool) -> dict[str, Any]:
    if path.is_dir():
        if not recursive:
            raise PathRefused(f"{path} 是目录；删除目录需要显式开启 recursive")
        # Refuse a directory that is a symlink: rmtree on one follows it.
        if path.is_symlink():
            path.unlink()
            return {"deleted": True, "path": str(path), "kind": "symlink"}
        count = sum(1 for _ in path.rglob("*"))
        shutil.rmtree(path)
        return {"deleted": True, "path": str(path), "kind": "dir", "entries": count}
    if not path.exists() and not path.is_symlink():
        return {"deleted": False, "path": str(path), "reason": "文件不存在"}
    path.unlink()
    return {"deleted": True, "path": str(path), "kind": "file"}


def collect(source: Path, pattern: str, recursive: bool) -> list[Path]:
    """Every file under ``source`` matching ``pattern``.

    An empty pattern matching everything is the dangerous case, so it is only
    reachable by asking for it: ``pattern="*"`` is explicit, and the caller
    decides whether the directory is small enough.
    """
    if source.is_file():
        return [source] if fnmatch.fnmatch(source.name, pattern) else []
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if fnmatch.fnmatch(name, pattern):
                found.append(Path(dirpath) / name)
        if not recursive:
            break
    return found


def summarise(entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    items = list(entries)
    return {
        "count": sum(1 for i in items if not i.get("truncated")),
        "total_bytes": sum(i.get("size", 0) for i in items if not i.get("truncated")),
        "truncated": any(i.get("truncated") for i in items),
        "entries": items,
    }
