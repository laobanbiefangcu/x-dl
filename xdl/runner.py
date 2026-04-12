from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

from .config import Settings
from .utils import MEDIA_SUFFIXES, make_proxies

logger = logging.getLogger(__name__)

# 输出中包含这些关键词时视为可重试的瞬时网络错误
_TRANSIENT_ERRORS = ("ssl", "eof", "connection", "timeout", "reset", "broken pipe", "network")


def run(
    url: str,
    settings: Settings,
    *,
    use_archive: bool = True,
    target_dir: Path | None = None,
    retries: int = 3,
) -> list[Path]:
    """Run gallery-dl for *url*, return newly downloaded media files sorted by path.

    Args:
        use_archive: 是否用 archive.db 去重（sync 模式开启；bot 按需下载关闭）
        target_dir:  覆盖下载目录，为 None 时使用 settings.download_dir
        retries:     遇到瞬时网络错误时的最大重试次数
    """
    base_dir = target_dir or settings.download_dir
    base_dir.mkdir(parents=True, exist_ok=True)

    if use_archive:
        settings.archive_file.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "gallery_dl",
        "--config", str(settings.gdl_config),
        "--cookies", settings.cookies_file,
        "--no-colors",
        "-d", str(base_dir),
    ]
    if use_archive:
        cmd += ["--download-archive", str(settings.archive_file)]
    proxies = make_proxies(settings.proxy)
    if proxies:
        cmd += ["--proxy", proxies["https"]]
    cmd.append(url)

    last_output = ""
    for attempt in range(1, retries + 1):
        before = _snapshot(base_dir)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        output = proc.stderr + proc.stdout

        if output.strip():
            logger.info(output.rstrip())

        if proc.returncode not in (0, 1):
            logger.warning("gallery-dl exited with code %d for %s", proc.returncode, url)

        # mtime 对比：新增文件 OR 被重新下载（覆盖）的文件
        after = _snapshot(base_dir)
        new_files = sorted(
            p for p, mt in after.items()
            if p not in before or before[p] != mt
        )
        if new_files:
            return new_files

        # 返回码 0/1 表示 gallery-dl 正常退出，无新文件即为"已是最新"
        if proc.returncode in (0, 1):
            return []

        last_output = output.strip()
        lower = last_output.lower()

        # 瞬时网络错误 → 等待后重试
        if any(k in lower for k in _TRANSIENT_ERRORS) and attempt < retries:
            wait = attempt * 3
            logger.warning("[runner] 网络错误，%ds 后重试（%d/%d）…", wait, attempt, retries)
            time.sleep(wait)
            continue

        break  # 非网络错误或已达最大重试次数

    # 分析最终错误原因（returncode >= 2）
    lower = last_output.lower()
    if "login" in lower or "log in" in lower or "auth" in lower:
        raise RuntimeError("需要登录，请重新导出 cookies.txt")
    if "no results" in lower or "no result" in lower:
        return []
    if "deleted" in lower:
        raise RuntimeError("推文无内容（可能已删除、纯文字或账号受限）")
    if any(k in lower for k in _TRANSIENT_ERRORS):
        raise RuntimeError(f"网络连接不稳定，重试 {retries} 次后仍失败，请检查代理")
    if last_output:
        raise RuntimeError(f"下载失败:\n{last_output[:300]}")
    return []


def _snapshot(directory: Path) -> dict[Path, float]:
    """返回目录内所有媒体文件的 path → mtime 映射。"""
    if not directory.exists():
        return {}
    return {
        p: p.stat().st_mtime
        for p in directory.rglob("*")
        if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES
    }
