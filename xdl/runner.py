"""gallery-dl 库调用包装 —— 相比子进程：

- 省掉每次几百毫秒的进程启动开销
- 通过 hooks 实时拿到每个下载完成的文件 + 推文元数据（正文、作者）
- 错误信息更结构化

代价是 gallery-dl 的 config 是全局的，所以同进程内串行执行（用 _gdl_lock）。
"""
from __future__ import annotations

import collections
import io
import logging
import re
import sys
import time
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from gallery_dl import config as gdl_config
from gallery_dl import exception as gdl_exception
from gallery_dl import job as gdl_job

from .config import Settings
from .utils import MEDIA_SUFFIXES

logger = logging.getLogger(__name__)

# gallery-dl 用全局 config，所以一次只能跑一个 job
_gdl_lock = threading.Lock()

# 用于把瞬时网络错误识别出来后允许重试
_TRANSIENT_RE = re.compile(
    r"\b("
    r"timed?out|timeout|"
    r"connection (?:reset|aborted|refused|closed|error)|"
    r"connection broken|"
    r"name resolution|"
    r"temporary failure|"
    r"ssl(?:error|_)?|ssl handshake|"
    r"eof occurred|"
    r"broken pipe|"
    r"read timed out|"
    r"max retries exceeded|"
    r"remote end closed|"
    r"5\d\d (?:server error|service unavailable|gateway)"
    r")\b",
    re.I,
)

_AUTH_RE = re.compile(r"\b(login|log in|authenticat\w*|not authorized|unauthorized)\b", re.I)
_DELETED_RE = re.compile(r"\b(deleted|not found|tweet is unavailable|nsfw)\b", re.I)


@dataclass
class TweetMeta:
    tweet_id: str = ""
    author: str = ""
    text: str = ""
    url: str = ""


@dataclass
class RunResult:
    new_files: list[Path] = field(default_factory=list)
    tweets: dict[str, TweetMeta] = field(default_factory=dict)
    skipped: int = 0
    failed: bool = False

    def meta_for(self, tweet_id: str) -> TweetMeta:
        return self.tweets.get(tweet_id, TweetMeta(tweet_id=tweet_id))


ProgressCallback = Callable[[str, Path], None]  # event, path


class _LogCapture(logging.Handler):
    """收集 gallery-dl 的 WARNING/ERROR，方便错误归因。"""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(record.getMessage())
        except Exception:
            pass

    def joined(self) -> str:
        return "\n".join(self.lines)


def run(
    url: str,
    settings: Settings,
    *,
    use_archive: bool = True,
    target_dir: Path | None = None,
    retries: int = 3,
    progress_cb: ProgressCallback | None = None,
) -> RunResult:
    """跑一个 gallery-dl job，返回新增文件 + 推文元数据。

    Args:
        use_archive: True 时启用 archive.db 全局去重
        target_dir:  覆盖下载目录
        retries:     遇到瞬时网络错误时的最大重试次数
        progress_cb: 每个事件触发一次（"file"|"skip"，path）
    """
    base_dir = target_dir or settings.download_dir
    base_dir.mkdir(parents=True, exist_ok=True)
    if use_archive:
        settings.archive_file.parent.mkdir(parents=True, exist_ok=True)

    last_error = ""
    for attempt in range(1, retries + 1):
        result, raw = _run_once(url, settings, base_dir, use_archive, progress_cb)
        if not result.failed:
            return result

        lower = raw.lower()
        if _TRANSIENT_RE.search(lower) and attempt < retries:
            wait = attempt * 3
            logger.warning("[runner] 网络错误，%ds 后重试（%d/%d）…", wait, attempt, retries)
            time.sleep(wait)
            last_error = raw
            continue

        last_error = raw
        break

    lower = last_error.lower()
    if _AUTH_RE.search(lower):
        raise RuntimeError("需要登录，请重新导出 cookies.txt")
    if _DELETED_RE.search(lower):
        raise RuntimeError("推文无内容（可能已删除、纯文字或账号受限）")
    if _TRANSIENT_RE.search(lower):
        raise RuntimeError(f"网络连接不稳定，重试 {retries} 次后仍失败，请检查代理")
    if last_error:
        raise RuntimeError(f"下载失败:\n{last_error[:300]}")
    raise RuntimeError("下载失败（未知原因）")


def _run_once(
    url: str,
    settings: Settings,
    base_dir: Path,
    use_archive: bool,
    progress_cb: ProgressCallback | None,
) -> tuple[RunResult, str]:
    result = RunResult()
    capture = _LogCapture()
    gdl_root = logging.getLogger("gallery_dl")
    gdl_root.addHandler(capture)

    def on_after(pathfmt) -> None:
        path = Path(pathfmt.path)
        if path.suffix.lower() not in MEDIA_SUFFIXES:
            return
        result.new_files.append(path)
        tweet_id, meta = _extract_tweet_meta(pathfmt, url)
        if tweet_id:
            existing = result.tweets.get(tweet_id)
            if existing:
                # 更新缺失字段
                for k in ("author", "text", "url"):
                    if not getattr(existing, k) and getattr(meta, k):
                        setattr(existing, k, getattr(meta, k))
            else:
                result.tweets[tweet_id] = meta
        if progress_cb:
            try:
                progress_cb("file", path)
            except Exception:
                pass

    def on_skip(pathfmt) -> None:
        result.skipped += 1
        if progress_cb:
            try:
                progress_cb("skip", Path(pathfmt.path or ""))
            except Exception:
                pass

    with _gdl_lock:
        try:
            gdl_config.clear()
            gdl_config.load([str(settings.gdl_config)])
            gdl_config.set((), "base-directory", str(base_dir))
            gdl_config.set((), "cookies", settings.cookies_file)
            if settings.proxy:
                gdl_config.set((), "proxy", settings.proxy)
            if use_archive:
                gdl_config.set((), "archive", str(settings.archive_file))

            j = gdl_job.DownloadJob(url)
            j.hooks = collections.defaultdict(list)
            j.hooks["after"].append(on_after)
            j.hooks["skip"].append(on_skip)

            rc = j.run()
            # gallery-dl: 0=成功 1=部分跳过 4=下载失败 8=认证
            if rc and rc >= 4:
                result.failed = True
        except gdl_exception.AuthenticationError as exc:
            result.failed = True
            capture.lines.append(f"authentication: {exc}")
        except gdl_exception.HttpError as exc:
            result.failed = True
            capture.lines.append(f"http: {exc}")
        except gdl_exception.GalleryDLException as exc:
            result.failed = True
            capture.lines.append(str(exc))
        except Exception as exc:
            result.failed = True
            capture.lines.append(f"unexpected: {exc}\n{traceback.format_exc(limit=2)}")
        finally:
            gdl_root.removeHandler(capture)

    return result, capture.joined()


def _extract_tweet_meta(pathfmt, source_url: str) -> tuple[str, TweetMeta]:
    """从 gallery-dl 的 kwdict 抽取作者/正文/tweet_id。"""
    kw = getattr(pathfmt, "kwdict", None) or {}
    tweet_id = str(kw.get("tweet_id") or kw.get("id") or "").strip()
    author = ""
    a = kw.get("author")
    if isinstance(a, dict):
        author = (a.get("name") or a.get("nick") or "").strip()
    elif isinstance(a, str):
        author = a.strip()
    if not author:
        author = (kw.get("user", {}) or {}).get("name", "") if isinstance(kw.get("user"), dict) else ""
    text = (kw.get("content") or kw.get("description") or "").strip()
    url = ""
    if tweet_id and author:
        url = f"https://x.com/{author}/status/{tweet_id}"
    elif tweet_id:
        url = source_url
    return tweet_id, TweetMeta(tweet_id=tweet_id, author=author, text=text, url=url)
