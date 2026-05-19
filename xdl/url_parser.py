"""可扩展的 URL 路由表 —— gallery-dl 本身支持几十个站点，这里允许 bot 处理它们。

X / Twitter 仍是默认主力；其他平台只要 gallery-dl 能下载，bot 就能收。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_TRAILING_PUNCT = re.compile(r"[.,!?;:'\"()（）。，！？]+$")


@dataclass(frozen=True)
class Platform:
    name: str
    pattern: re.Pattern[str]


_PLATFORMS: tuple[Platform, ...] = (
    Platform("x",         re.compile(r"https?://(?:www\.)?(?:x|twitter|fxtwitter|vxtwitter)\.com/\S+/status/\d+\S*")),
    Platform("pixiv",     re.compile(r"https?://(?:www\.)?pixiv\.net/(?:en/)?artworks/\d+\S*")),
    Platform("instagram", re.compile(r"https?://(?:www\.)?instagram\.com/(?:p|reel|tv)/[\w-]+/?\S*")),
    Platform("weibo",     re.compile(r"https?://(?:www\.)?weibo\.com/\d+/\w+\S*")),
    Platform("reddit",    re.compile(r"https?://(?:www\.)?reddit\.com/r/\w+/comments/\w+\S*")),
    Platform("bilibili",  re.compile(r"https?://(?:www\.)?bilibili\.com/video/[\w]+\S*")),
    Platform("youtube",   re.compile(r"https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[\w-]+\S*")),
)


def find(text: str) -> list[tuple[str, str]]:
    """返回 [(platform, url), ...]，按 text 中出现顺序去重。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for p in _PLATFORMS:
        for raw in p.pattern.findall(text):
            url = _TRAILING_PUNCT.sub("", raw)
            url = _normalize(p.name, url)
            if url in seen:
                continue
            seen.add(url)
            out.append((p.name, url))
    return out


def _normalize(platform: str, url: str) -> str:
    if platform == "x":
        url = url.replace("//twitter.com/", "//x.com/")
        url = url.replace("//fxtwitter.com/", "//x.com/")
        url = url.replace("//vxtwitter.com/", "//x.com/")
        url = re.sub(r"\?.*$", "", url)
    return url
