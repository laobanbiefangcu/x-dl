"""磁盘配额管理。超过上限时按 mtime 删旧文件，并清理空目录。"""
from __future__ import annotations

from pathlib import Path

from .utils import MEDIA_SUFFIXES, cleanup_empty_dirs


def dir_size_bytes(directory: Path) -> int:
    if not directory.exists():
        return 0
    return sum(
        p.stat().st_size
        for p in directory.rglob("*")
        if p.is_file()
    )


def enforce_quota(directory: Path, max_gb: float) -> tuple[int, int]:
    """若 max_gb > 0 且目录占用超限，按 mtime 升序删旧文件直到回到上限。

    返回 (删除文件数, 释放字节数)。
    """
    if max_gb <= 0 or not directory.exists():
        return 0, 0

    limit = int(max_gb * 1024 * 1024 * 1024)
    cur = dir_size_bytes(directory)
    if cur <= limit:
        return 0, 0

    candidates = sorted(
        (p for p in directory.rglob("*")
         if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES),
        key=lambda p: p.stat().st_mtime,
    )
    deleted = freed = 0
    for f in candidates:
        if cur - freed <= limit:
            break
        try:
            size = f.stat().st_size
            f.unlink()
            cleanup_empty_dirs(f, directory)
            deleted += 1
            freed += size
        except OSError:
            continue
    return deleted, freed
