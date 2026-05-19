"""共享工具函数。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

MEDIA_SUFFIXES: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".mp4", ".mov", ".m4v", ".webm", ".mkv",
})

_FNAME_RE = re.compile(r"^([^_]+)_(\d{10,})_\d+\.")


def parse_filename(path: Path) -> tuple[str | None, str | None]:
    """返回 (author, tweet_id)，匹配 gallery-dl 的 twitter filename 模板。"""
    m = _FNAME_RE.match(path.name)
    if m:
        return m.group(1), m.group(2)
    return None, None


def caption(path: Path, tweet_text: str = "") -> str:
    """构造发送给 Telegram 的 caption。"""
    author, tweet_id = parse_filename(path)
    if author and tweet_id:
        link = f"https://x.com/{author}/status/{tweet_id}"
        if tweet_text:
            text = tweet_text.strip()
            if len(text) > 800:  # Telegram caption 上限 1024，给链接 + 用户名留余量
                text = text[:800].rstrip() + "…"
            return f"{text}\n\n@{author}\n{link}"
        return f"@{author}\n{link}"
    return path.stem


def group_by_tweet(files: list[Path]) -> dict[str, list[Path]]:
    groups: dict[str, list[Path]] = {}
    for f in files:
        _, tweet_id = parse_filename(f)
        key = tweet_id or f.stem
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
    parent = file_path.parent
    while parent != base_dir and parent != parent.parent:
        try:
            parent.rmdir()
            parent = parent.parent
        except OSError:
            break


def file_md5(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()
