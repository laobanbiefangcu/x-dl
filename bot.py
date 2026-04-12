#!/usr/bin/env python3
"""Telegram bot: 发推文/X 链接，自动下载媒体并回复。

用法:
    .venv/bin/python bot.py

.env 关键配置:
    TELEGRAM_BOT_TOKEN      必填
    COOKIES_FILE            gallery-dl 需要的 X cookies
    BOT_ALLOWED_CHAT_IDS    可选，逗号分隔的 chat_id 白名单；空则响应所有人
    BOT_MAX_WORKERS         并发下载线程数，默认 4
"""
from __future__ import annotations

import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import requests

from xdl.config import load_settings, Settings
from xdl import runner, telegram, cookies as cookie_checker

TWEET_RE = re.compile(
    r"https?://(?:x|twitter)\.com/\S+/status/\d+\S*"
)
_TRAILING_PUNCT = re.compile(r"[.,!?;:'\"()（）。，！？]+$")

_URL_DEDUP_TTL = 300.0  # 5 分钟内同一链接不重复下载


class _URLCache:
    def __init__(self, ttl: float = _URL_DEDUP_TTL) -> None:
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}
        self._ttl = ttl

    def is_duplicate(self, url: str) -> bool:
        """若 url 在 TTL 内已处理过返回 True，否则记录并返回 False。"""
        now = time.time()
        with self._lock:
            self._seen = {k: v for k, v in self._seen.items() if now - v < self._ttl}
            if url in self._seen:
                return True
            self._seen[url] = now
            return False

    def invalidate(self, url: str) -> None:
        """下载失败时移除记录，允许用户立即重试。"""
        with self._lock:
            self._seen.pop(url, None)


_url_cache = _URLCache()


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Telegram API wrapper
# ---------------------------------------------------------------------------

class TelegramBot:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._base = f"{settings.telegram_api_base.rstrip('/')}/bot{settings.telegram_bot_token}"
        self._proxies = _make_proxies(settings.proxy)

    def _call(self, method: str, _timeout: int = 30, _retries: int = 3, **body) -> dict:
        last_exc: Exception | None = None
        for attempt in range(1, _retries + 1):
            try:
                resp = requests.post(
                    f"{self._base}/{method}",
                    json=body,
                    proxies=self._proxies,
                    timeout=_timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                if not data.get("ok"):
                    raise RuntimeError(f"Telegram [{method}] error: {data}")
                return data["result"]
            except (requests.exceptions.SSLError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError) as exc:
                last_exc = exc
                if attempt < _retries:
                    time.sleep(attempt * 2)
                    continue
                raise
        raise last_exc  # type: ignore[misc]

    def get_updates(self, offset: int, poll_timeout: int = 20) -> list[dict]:
        try:
            return self._call(
                "getUpdates",
                _timeout=poll_timeout + 10,
                _retries=1,   # 长轮询不重试，失败直接回到下一轮
                offset=offset,
                timeout=poll_timeout,
                allowed_updates=["message"],
            )
        except (requests.exceptions.ReadTimeout, requests.exceptions.SSLError,
                requests.exceptions.ConnectionError):
            return []

    def send_message(self, chat_id: int | str, text: str, reply_to: int | None = None) -> int:
        body: dict = {"chat_id": chat_id, "text": text}
        if reply_to:
            body["reply_to_message_id"] = reply_to
        result = self._call("sendMessage", **body)
        return result["message_id"]

    def edit_message(self, chat_id: int | str, message_id: int, text: str) -> None:
        try:
            self._call("editMessageText", chat_id=chat_id, message_id=message_id, text=text)
        except Exception:
            pass

    def delete_message(self, chat_id: int | str, message_id: int) -> None:
        try:
            self._call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except Exception:
            pass

    def send_files(self, chat_id: int | str, files: list[Path], caption: str) -> None:
        telegram.send_files(
            files,
            bot_token=self.s.telegram_bot_token,
            chat_id=str(chat_id),
            caption=caption,
            api_base=self.s.telegram_api_base,
            proxy=self.s.proxy,
            max_upload_bytes=self.s.telegram_max_upload_bytes,
            split_oversized_video=self.s.telegram_split_oversized_video,
            compress_oversized_video=self.s.telegram_compress_oversized_video,
            ffmpeg_preset=self.s.telegram_ffmpeg_preset,
            rate_limit_seconds=self.s.telegram_rate_limit_seconds,
            send_retries=self.s.telegram_send_retries,
        )


# ---------------------------------------------------------------------------
# Per-URL task（在线程池中执行）
# ---------------------------------------------------------------------------

def _process_url(
    bot: TelegramBot,
    url: str,
    dest: str,
    notify_chat: str | None,
    reply_to: int,
) -> None:
    status_id: int | None = None
    if notify_chat:
        status_id = bot.send_message(notify_chat, "⏳ 下载中…", reply_to=reply_to)

    def _update(text: str) -> None:
        if notify_chat and status_id:
            bot.edit_message(notify_chat, status_id, text)

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            files = runner.run(
                url,
                bot.s,
                use_archive=False,
                target_dir=Path(tmpdir),
                retries=5,
            )
            if not files:
                _update("⚠️ 未找到媒体文件")
                _log(f"  [{url}] 无媒体文件")
                return

            _log(f"  [{url}] 下载完成 {len(files)} 个文件，发送中…")
            bot.send_files(dest, files, caption=url)
            _update(f"✅ 已发送（{len(files)} 个文件）")
            _log(f"  [{url}] 发送成功 → {dest}")

    except telegram.TelegramFileTooLargeError as exc:
        _update(f"⚠️ 文件过大: {exc}")
        _log(f"  [{url}] 文件过大: {exc}")
    except Exception as exc:  # noqa: BLE001
        _url_cache.invalidate(url)  # 允许用户立即重试
        _update(f"❌ 失败: {exc}")
        _log(f"  [{url}] 异常: {exc}")


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------

def handle_message(bot: TelegramBot, executor: ThreadPoolExecutor, message: dict) -> None:
    chat_id: int = message["chat"]["id"]
    msg_id: int = message["message_id"]
    sender = (message.get("from") or {}).get("username") or str(chat_id)
    text: str = message.get("text") or message.get("caption") or ""

    _log(f"收到消息 chat_id={chat_id} from=@{sender}: {text[:80]!r}")

    if bot.s.bot_allowed_chat_ids and str(chat_id) not in bot.s.bot_allowed_chat_ids:
        _log(f"  → 忽略（chat_id={chat_id} 不在白名单）")
        return

    urls = [_TRAILING_PUNCT.sub("", u) for u in TWEET_RE.findall(text)]
    if not urls:
        _log("  → 无推文链接，跳过")
        return

    urls = [u for u in urls if not _url_cache.is_duplicate(u)]
    if not urls:
        _log("  → 链接均在去重窗口内，跳过")
        return

    _log(f"  → 找到 {len(urls)} 个链接，并发下载")

    dest = bot.s.telegram_chat_id or str(chat_id)
    notify_chat = str(chat_id) if str(chat_id) != dest else None

    for url in urls:
        executor.submit(_process_url, bot, url, dest, notify_chat, msg_id)


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

def poll(bot: TelegramBot, max_workers: int) -> None:
    _log(f"Bot 已启动，并发线程数: {max_workers}，正在监听消息… (Ctrl+C 退出)")
    offset = 0
    backoff = 0  # 连续失败次数，用于指数退避
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        while True:
            try:
                updates = bot.get_updates(offset=offset)
                backoff = 0  # 成功则重置退避
                for update in updates:
                    offset = update["update_id"] + 1
                    if "message" in update:
                        handle_message(bot, executor, update["message"])
            except KeyboardInterrupt:
                _log("退出，等待进行中的任务完成…")
                break
            except Exception as exc:  # noqa: BLE001
                backoff = min(backoff + 1, 6)  # 最长 64s
                wait = 2 ** backoff
                _log(f"轮询异常（{wait}s 后重试）: {exc}")
                time.sleep(wait)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_proxies(proxy: str) -> dict[str, str] | None:
    p = proxy.strip()
    if not p:
        return None
    if p.startswith("socks5://"):
        p = "socks5h://" + p[len("socks5://"):]
    return {"http": p, "https": p}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    settings = load_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("错误: TELEGRAM_BOT_TOKEN 未在 .env 中配置")
    if not Path(settings.cookies_file).exists():
        raise SystemExit(f"错误: cookies 文件不存在: {settings.cookies_file}")

    max_workers = int(os.getenv("BOT_MAX_WORKERS", "4"))

    bot = TelegramBot(settings)
    try:
        info = bot._call("getMe")
    except Exception as exc:
        raise SystemExit(f"无法连接 Telegram: {exc}") from exc

    _log(f"已连接: @{info['username']} ({info['first_name']})")
    _log("白名单 chat_id: " + ", ".join(settings.bot_allowed_chat_ids) if settings.bot_allowed_chat_ids else "无白名单，响应所有人")

    def _check_cookies() -> None:
        ok, msg = cookie_checker.check(
            settings.cookies_file, settings.proxy, str(settings.gdl_config)
        )
        _log(f"[cookies] {'✓' if ok else '✗'} {msg}")

    _log("正在检测 cookies 有效性（后台）…")
    threading.Thread(target=_check_cookies, daemon=True).start()

    poll(bot, max_workers)


if __name__ == "__main__":
    main()
