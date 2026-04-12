"""Cookies 有效性检测。"""
from __future__ import annotations

import http.cookiejar
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


def check(cookies_file: str, proxy: str = "", gdl_config: str = "") -> tuple[bool, str]:
    """检测 cookies 是否有效。

    Returns:
        (ok, message)  ok=False 时 message 说明原因
    """
    # ── 1. 文件存在 ──────────────────────────────────────────
    path = Path(cookies_file)
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

    # ── 5. 用 gallery-dl 实际请求验证（与下载走同一认证路径）──
    cmd = [
        sys.executable, "-m", "gallery_dl",
        "--cookies", cookies_file,
        "--no-colors",
        "--print", "{username}",
    ]
    if gdl_config:
        cmd += ["--config", gdl_config]
    if proxy.strip():
        p = proxy.strip()
        if p.startswith("socks5://"):
            p = "socks5h://" + p[len("socks5://"):]
        cmd += ["--proxy", p]
    cmd.append("https://x.com/i/bookmarks")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        output = (proc.stdout + proc.stderr).lower()

        if "login" in output or "log in" in output:
            return False, "cookies 已失效（需要重新登录），请重新导出"
        if "auth" in output and "error" in output:
            return False, "cookies 认证失败，请重新导出"
        # "no results" = 书签为空但认证成功；有内容 = 直接成功
        return True, "认证有效"

    except subprocess.TimeoutExpired:
        return True, "验证超时，跳过网络验证（本地检查通过）"
    except Exception as exc:  # noqa: BLE001
        return True, f"验证异常，视为有效: {exc}"
