"""Shared utilities used by both bot.py and sync.py."""
from __future__ import annotations

import re
from pathlib import Path

MEDIA_SUFFIXES: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".mp4", ".mov", ".m4v", ".webm", ".mkv",
})

_FNAME_RE = re.compile(r"^([^_]+)_(\d{10,})_\d+\.")


def caption(path: Path) -> str:
    m = _FNAME_RE.match(path.name)
    if m:
        author, tweet_id = m.group(1), m.group(2)
        return f"@{author}\nhttps://x.com/{author}/status/{tweet_id}"
    return path.stem


def group_by_tweet(files: list[Path]) -> dict[str, list[Path]]:
    """同一条推文的多个媒体文件归为一组。"""
    groups: dict[str, list[Path]] = {}
    for f in files:
        m = _FNAME_RE.match(f.name)
        key = m.group(2) if m else f.stem
        groups.setdefault(key, []).append(f)
    return groups


def make_proxies(proxy: str) -> dict[str, str] | None:
    p = proxy.strip()
    if not p:
        return None
    if p.startswith("socks5://"):
        p = "socks5h://" + p[len("socks5://"):]
    return {"http": p, "https": p}


def cleanup_empty_dirs(file_path: Path, base_dir: Path) -> None:
    """删除文件后，向上递归清理空目录，直到 base_dir 为止。"""
    parent = file_path.parent
    while parent != base_dir and parent != parent.parent:
        try:
            parent.rmdir()  # 非空时会抛 OSError，自动停止
            parent = parent.parent
        except OSError:
            break
