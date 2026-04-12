"""Cookies 有效性检测。"""
from __future__ import annotations

import http.cookiejar
import logging
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .utils import make_proxies

logger = logging.getLogger(__name__)

_cache_lock = threading.Lock()
_check_cache: dict[str, tuple[float, float, bool, str]] = {}
# key -> (cached_at, file_mtime, ok, msg)
_CACHE_TTL = 3600.0  # 1 小时内同一文件不重复做网络验证


def check(cookies_file: str, proxy: str = "", gdl_config: str = "") -> tuple[bool, str]:
    """检测 cookies 是否有效。

    Returns:
        (ok, message)  ok=False 时 message 说明原因
    """
    path = Path(cookies_file)

    # ── 0. 缓存命中 ───────────────────────────────────────────
    try:
        cur_mtime = path.stat().st_mtime
    except OSError:
        cur_mtime = 0.0

    now = time.time()
    with _cache_lock:
        entry = _check_cache.get(cookies_file)
        if entry:
            cached_at, cached_mtime, ok, msg = entry
            if now - cached_at < _CACHE_TTL and cur_mtime == cached_mtime:
                return ok, msg

    result = _do_check(cookies_file, proxy, gdl_config, path, cur_mtime)
    with _cache_lock:
        _check_cache[cookies_file] = (now, cur_mtime, *result)
    return result


def invalidate_cache(cookies_file: str) -> None:
    """cookies 文件更新后主动失效缓存。"""
    with _cache_lock:
        _check_cache.pop(cookies_file, None)


def days_until_expiry(cookies_file: str) -> int | None:
    """返回 auth_token / ct0 中最快到期的剩余天数，无法确定时返回 None。"""
    path = Path(cookies_file)
    if not path.exists():
        return None
    jar = http.cookiejar.MozillaCookieJar()
    try:
        jar.load(str(path), ignore_discard=True, ignore_expires=True)
    except Exception:
        return None

    now = time.time()
    min_days: float | None = None
    for c in jar:
        if c.name in ("auth_token", "ct0") and c.expires:
            days = (c.expires - now) / 86400
            if min_days is None or days < min_days:
                min_days = days
    return int(min_days) if min_days is not None else None


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _do_check(
    cookies_file: str, proxy: str, gdl_config: str,
    path: Path, cur_mtime: float,
) -> tuple[bool, str]:
    # ── 1. 文件存在 ──────────────────────────────────────────
    if not path.exists():
        return False, f"cookies 文件不存在: {cookies_file}"

    # ── 2. 格式可解析 ─────────────────────────────────────────
    jar = http.cookiejar.MozillaCookieJar()
    try:
        jar.load(str(path), ignore_discard=True, ignore_expires=True)
    except Exception as exc:
        return False, f"cookies 格式错误: {exc}"

    by_name = {c.name: c for c in jar}

    # ── 3. 关键字段存在 ───────────────────────────────────────
    for field in ("auth_token", "ct0"):
        if field not in by_name:
            return False, f"cookies 缺少 {field}，请重新导出"

    # ── 4. 未超过到期日 ───────────────────────────────────────
    now = time.time()
    for field in ("auth_token", "ct0"):
        exp = by_name[field].expires
        if exp and exp < now:
            exp_str = datetime.fromtimestamp(exp).strftime("%Y-%m-%d")
            return False, f"cookies 已到期（{field} 到期: {exp_str}），请重新导出"

    # ── 5. 用 gallery-dl 实际请求验证 ─────────────────────────
    cmd = [
        sys.executable, "-m", "gallery_dl",
        "--cookies", cookies_file,
        "--no-colors",
        "--print", "{username}",
    ]
    if gdl_config:
        cmd += ["--config", gdl_config]
    proxies = make_proxies(proxy)
    if proxies:
        cmd += ["--proxy", proxies["https"]]
    cmd.append("https://x.com/i/bookmarks")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        output = (proc.stdout + proc.stderr).lower()
        if "login" in output or "log in" in output:
            logger.debug("[cookies] gallery-dl output: %s", proc.stderr[:200])
            return False, "cookies 已失效（需要重新登录），请重新导出"
        if "auth" in output and "error" in output:
            logger.debug("[cookies] gallery-dl output: %s", proc.stderr[:200])
            return False, "cookies 认证失败，请重新导出"
        return True, "认证有效"
    except subprocess.TimeoutExpired:
        return True, "验证超时，跳过网络验证（本地检查通过）"
    except Exception as exc:  # noqa: BLE001
        return True, f"验证异常，视为有效: {exc}"
