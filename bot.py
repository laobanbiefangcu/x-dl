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

import logging
import os
import re
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import requests

from xdl.config import load_settings, Settings
from xdl import runner, telegram, cookies as cookie_checker
from xdl.utils import caption as _caption, group_by_tweet as _group_by_tweet, make_proxies, MEDIA_SUFFIXES, cleanup_empty_dirs

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
    force=True,
)
_logger = logging.getLogger("x-dl.bot")

TWEET_RE = re.compile(
    r"https?://(?:x|twitter)\.com/\S+/status/\d+\S*"
)
_TRAILING_PUNCT = re.compile(r"[.,!?;:'\"()（）。，！？]+$")


# ---------------------------------------------------------------------------
# Sync state（追踪上次同步情况，供 /status 使用）
# ---------------------------------------------------------------------------

class _SyncStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.last_time: datetime | None = None
        self.last_count: int = 0
        self.last_error: str | None = None

    def set_success(self, count: int) -> None:
        with self._lock:
            self.last_time = datetime.now()
            self.last_count = count
            self.last_error = None

    def set_error(self, error: str) -> None:
        with self._lock:
            self.last_error = error

    def snapshot(self) -> tuple[datetime | None, int, str | None]:
        with self._lock:
            return self.last_time, self.last_count, self.last_error

_sync_stats = _SyncStats()

_URL_DEDUP_TTL = 300.0  # 5 分钟内同一链接不重复下载


class _URLCache:
    _MAXSIZE = 2000  # 防止长时间运行内存无限增长

    def __init__(self, ttl: float = _URL_DEDUP_TTL) -> None:
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}
        self._ttl = ttl

    def is_duplicate(self, url: str) -> bool:
        """若 url 在 TTL 内已处理过返回 True，否则记录并返回 False。"""
        now = time.time()
        with self._lock:
            self._seen = {k: v for k, v in self._seen.items() if now - v < self._ttl}
            if len(self._seen) >= self._MAXSIZE:
                # 超出上限时移除最旧的一批
                oldest = sorted(self._seen, key=self._seen.__getitem__)[:self._MAXSIZE // 4]
                for k in oldest:
                    del self._seen[k]
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
    _logger.info(msg)


# ---------------------------------------------------------------------------
# Telegram API wrapper
# ---------------------------------------------------------------------------

class TelegramBot:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._base = f"{settings.telegram_api_base.rstrip('/')}/bot{settings.telegram_bot_token}"
        self._proxies = make_proxies(settings.proxy)

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
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"Telegram [{method}] failed after {_retries} retries")

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
        except Exception as exc:
            _logger.debug("edit_message failed: %s", exc)

    def delete_message(self, chat_id: int | str, message_id: int) -> None:
        try:
            self._call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except Exception as exc:
            _logger.debug("delete_message failed: %s", exc)

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
# Shared send helper
# ---------------------------------------------------------------------------

def _send_groups(
    bot: TelegramBot,
    groups: dict[str, list[Path]],
    dest: str,
    status_chat: str,
    status_id: int,
    *,
    delete: bool = False,
    download_dir: Path | None = None,
    tag: str = "send",
) -> int:
    """逐组发送推文媒体，实时更新进度消息，返回成功发送条数。"""
    total = len(groups)
    sent = 0
    for i, (tweet_id, files) in enumerate(groups.items(), 1):
        bot.edit_message(status_chat, status_id, f"📤 发送中 {i}/{total}…")
        cap = _caption(files[0])
        try:
            bot.send_files(dest, files, cap)
            sent += 1
            if delete and download_dir:
                for f in files:
                    f.unlink(missing_ok=True)
                    cleanup_empty_dirs(f, download_dir)
        except telegram.TelegramFileTooLargeError as exc:
            _log(f"[{tag}] {tweet_id} 文件过大: {exc}")
        except Exception as exc:  # noqa: BLE001
            _log(f"[{tag}] {tweet_id} 发送失败: {exc}")
        time.sleep(bot.s.telegram_rate_limit_seconds)
    return sent


# ---------------------------------------------------------------------------
# Sync task（在线程池中执行）
# ---------------------------------------------------------------------------

_sync_lock = threading.Lock()


def _run_sync(bot: TelegramBot, chat_id: str, reply_to: int, target: str | None = None) -> None:
    if not _sync_lock.acquire(blocking=False):
        bot.send_message(chat_id, "⏳ 同步已在进行中，请稍候…", reply_to=reply_to)
        return

    status_id: int | None = None
    try:
        label = {"likes": "点赞", "bookmarks": "书签"}.get(target or "", "书签/点赞")
        status_id = bot.send_message(chat_id, f"⏳ 正在同步{label}…", reply_to=reply_to)
        all_urls = bot.s.target_urls()
        if target == "likes":
            urls = [u for u in all_urls if "/likes" in u]
        elif target == "bookmarks":
            urls = [u for u in all_urls if "bookmarks" in u]
        else:
            urls = all_urls
        if not urls:
            bot.edit_message(chat_id, status_id, f"⚠️ 未配置{label}目标，请检查 .env 中的 SYNC_TARGETS")
            return
        all_new: list[Path] = []
        for url in urls:
            _log(f"[sync] → {url}")
            new_files = runner.run(url, bot.s)
            all_new.extend(new_files)

        if not all_new:
            bot.edit_message(chat_id, status_id, "✅ 同步完成，无新内容")
            return

        groups = _group_by_tweet(all_new)
        dest = bot.s.telegram_chat_id or chat_id
        bot.edit_message(chat_id, status_id, f"📤 发现 {len(all_new)} 个新文件（{len(groups)} 条推文），发送中…")

        sent = _send_groups(
            bot, groups, dest, chat_id, status_id,
            delete=bot.s.delete_after_telegram,
            download_dir=bot.s.download_dir,
            tag="sync",
        )
        _sync_stats.set_success(sent)
        bot.edit_message(chat_id, status_id, f"✅ 同步完成，共发送 {sent} 条推文")
    except Exception as exc:  # noqa: BLE001
        _log(f"[sync] 异常: {exc}")
        _sync_stats.set_error(str(exc))
        msg = f"❌ 同步失败: {exc}"
        if status_id:
            bot.edit_message(chat_id, status_id, msg)
        else:
            bot.send_message(chat_id, msg, reply_to=reply_to)
    finally:
        _sync_lock.release()


def _clear_archive(bot: TelegramBot, chat_id: str, reply_to: int) -> None:
    if not _sync_lock.acquire(blocking=False):
        bot.send_message(chat_id, "⏳ 同步正在进行中，请等待完成后再清空", reply_to=reply_to)
        return

    try:
        download_dir = bot.s.download_dir
        local_files = sorted(
            f for f in download_dir.rglob("*")
            if f.is_file() and f.suffix.lower() in MEDIA_SUFFIXES
        ) if download_dir.exists() else []

        dest = bot.s.telegram_chat_id or chat_id

        if local_files:
            groups = _group_by_tweet(local_files)
            status_id = bot.send_message(
                chat_id,
                f"📤 发现 {len(local_files)} 个本地文件（{len(groups)} 条推文），发送到频道后清空…",
                reply_to=reply_to,
            )
            sent = _send_groups(
                bot, groups, dest, chat_id, status_id,
                delete=True, download_dir=download_dir, tag="clear",
            )
            bot.edit_message(chat_id, status_id, f"✅ 已发送 {sent} 条推文并删除本地文件")
        else:
            bot.send_message(chat_id, "📭 本地无媒体文件", reply_to=reply_to)

        archive = bot.s.archive_file
        if archive.exists():
            archive.unlink()
            _log("[clear] archive.db 已删除")
        bot.send_message(chat_id, "🗑 下载记录已清空，下次同步将重新获取所有内容")
    finally:
        _sync_lock.release()


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

    cmd_text = text.strip().lower().split("@")[0]  # 去掉 @botname 后缀
    if cmd_text in ("/sync_likes", "/sync likes"):
        _log("  → sync likes 命令")
        executor.submit(_run_sync, bot, str(chat_id), msg_id, "likes")
        return
    if cmd_text in ("/sync_bookmarks", "/sync bookmarks"):
        _log("  → sync bookmarks 命令")
        executor.submit(_run_sync, bot, str(chat_id), msg_id, "bookmarks")
        return
    if cmd_text == "/sync":
        _log("  → /sync 命令，同步全部")
        executor.submit(_run_sync, bot, str(chat_id), msg_id, None)
        return
    if cmd_text == "/clear":
        _log("  → /clear 命令，清空 archive")
        executor.submit(_clear_archive, bot, str(chat_id), msg_id)
        return
    if cmd_text == "/status":
        _log("  → /status 命令")
        executor.submit(_handle_status, bot, str(chat_id), msg_id)
        return
    if cmd_text == "/restart":
        _log("  → /restart 命令")
        executor.submit(_handle_restart, bot, str(chat_id), msg_id)
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
# /status
# ---------------------------------------------------------------------------

def _handle_status(bot: TelegramBot, chat_id: str, reply_to: int) -> None:
    ok, msg = cookie_checker.check(bot.s.cookies_file, bot.s.proxy, str(bot.s.gdl_config))
    days = cookie_checker.days_until_expiry(bot.s.cookies_file)

    cookie_line = f"{'✓' if ok else '✗'} {msg}"
    if days is not None and days > 0:
        cookie_line += f"（{days} 天后到期）"
    elif days is not None and days <= 0:
        cookie_line += "（已到期）"

    last_time, last_count, last_error = _sync_stats.snapshot()
    sync_line = f"{last_time.strftime('%m-%d %H:%M')}，+{last_count} 个文件" if last_time else "从未同步"
    syncing = "是" if _sync_lock.locked() else "否"

    lines = [
        "📊 状态",
        f"🍪 cookies: {cookie_line}",
        f"🔄 上次同步: {sync_line}",
        f"⏳ 同步中: {syncing}",
    ]
    if last_error:
        lines.append(f"❌ 上次错误: {last_error[:100]}")

    bot.send_message(chat_id, "\n".join(lines), reply_to=reply_to)


# ---------------------------------------------------------------------------
# /restart
# ---------------------------------------------------------------------------

def _handle_restart(bot: TelegramBot, chat_id: str, reply_to: int) -> None:
    bot.send_message(chat_id, "🔄 正在重启，约5秒后恢复…", reply_to=reply_to)
    _log("收到 /restart 命令，主动退出等待 systemd 重启")
    sys.exit(0)


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

    try:
        bot._call("setMyCommands", commands=[
            {"command": "sync",           "description": "同步所有目标（书签 + 点赞）"},
            {"command": "sync_likes",     "description": "只同步点赞"},
            {"command": "sync_bookmarks", "description": "只同步书签"},
            {"command": "clear",          "description": "发送本地文件到频道并清空下载记录"},
            {"command": "status",         "description": "查看 cookies 状态和上次同步信息"},
            {"command": "restart",        "description": "重启 Bot 服务"},
        ])
        _log("命令菜单已注册")
    except Exception as exc:
        _log(f"[warn] 注册命令菜单失败: {exc}")

    def _check_cookies() -> None:
        ok, msg = cookie_checker.check(
            settings.cookies_file, settings.proxy, str(settings.gdl_config)
        )
        _log(f"[cookies] {'✓' if ok else '✗'} {msg}")

    _log("正在检测 cookies 有效性（后台）…")
    threading.Thread(target=_check_cookies, daemon=True).start()

    threading.Thread(target=_cookies_monitor, args=(bot,), daemon=True).start()

    poll(bot, max_workers)


def _cookies_monitor(bot: TelegramBot) -> None:
    """每 12 小时检查一次 cookies，快到期或已失效时发 Telegram 通知。"""
    _WARN_DAYS = 7
    _CHECK_INTERVAL = 12 * 3600

    time.sleep(60)  # 等 bot 完全启动后再首次检查
    while True:
        targets = bot.s.bot_allowed_chat_ids or (
            [bot.s.telegram_chat_id] if bot.s.telegram_chat_id else []
        )
        if targets:
            days = cookie_checker.days_until_expiry(bot.s.cookies_file)
            if days is not None and days <= 0:
                _notify_all(bot, targets, "🔴 cookies 已到期，请立即更新！sync 已停止工作。")
            elif days is not None and days < _WARN_DAYS:
                _notify_all(bot, targets, f"⚠️ cookies 将在 {days} 天后到期，请及时更新")
        time.sleep(_CHECK_INTERVAL)


def _notify_all(bot: TelegramBot, targets: list[str], text: str) -> None:
    for t in targets:
        try:
            bot.send_message(t, text)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
