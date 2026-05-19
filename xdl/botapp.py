"""aiogram 3.x bot —— 长轮询 / webhook 双模式，内置定时同步、订阅、检索、失败队列。

所有阻塞操作（gallery-dl 调用、ffmpeg、磁盘 IO）通过 run_in_executor 丢到线程池，
不会阻塞 asyncio 事件循环。
"""
from __future__ import annotations

import asyncio
import html
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from . import cookies as cookie_checker
from . import db as db_module
from . import disk
from . import logging_setup
from . import runner
from . import telegram as tg_send
from . import url_parser
from .config import Settings, load_settings
from .utils import caption as build_caption
from .utils import (
    MEDIA_SUFFIXES,
    cleanup_empty_dirs,
    file_md5,
    group_by_tweet,
    parse_filename,
)

log = logging_setup.get("bot")


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------


class _State:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = db_module.get(settings.db_file)
        self.executor = ThreadPoolExecutor(
            max_workers=settings.bot_max_workers,
            thread_name_prefix="xdl-worker",
        )
        self.sync_lock = asyncio.Lock()
        self.started_at = time.time()
        self.processed = 0  # 本次运行处理过的链接数
        self.loop: asyncio.AbstractEventLoop | None = None

    async def run_blocking(self, fn, /, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self.executor, lambda: fn(*args, **kwargs)
        )


_state: _State  # set in main()


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------


def _allowed(chat_id: int) -> bool:
    allow = _state.settings.bot_allowed_chat_ids
    return not allow or str(chat_id) in allow


# ---------------------------------------------------------------------------
# Progress message helper
# ---------------------------------------------------------------------------


class _Status:
    """安全更新一条 status 消息，限制 edit 频率（avoid Telegram 429）。"""

    def __init__(self, bot: Bot, chat_id: int, reply_to: int) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.reply_to = reply_to
        self.message_id: int | None = None
        self._last_text = ""
        self._last_edit = 0.0
        self._min_interval = 1.0

    async def send(self, text: str) -> None:
        msg = await self.bot.send_message(
            self.chat_id, text, reply_to_message_id=self.reply_to
        )
        self.message_id = msg.message_id
        self._last_text = text
        self._last_edit = time.time()

    async def update(self, text: str, *, force: bool = False) -> None:
        if not self.message_id or text == self._last_text:
            return
        now = time.time()
        if not force and now - self._last_edit < self._min_interval:
            return
        try:
            await self.bot.edit_message_text(
                text, chat_id=self.chat_id, message_id=self.message_id
            )
            self._last_text = text
            self._last_edit = now
        except Exception as exc:  # 旧消息可能被删 / Telegram 偶尔 400
            log.debug("status update failed: {}", exc)


# ---------------------------------------------------------------------------
# Sending logic
# ---------------------------------------------------------------------------


async def _send_groups(
    bot: Bot,
    groups: dict[str, list[Path]],
    *,
    dest_chat_id: str,
    status: _Status | None,
    tweet_meta: dict[str, runner.TweetMeta] | None = None,
    delete_after: bool = False,
    tag: str = "send",
) -> int:
    """逐组发送一条推文的全部媒体，返回成功发送的推文数。"""
    s = _state.settings
    total = len(groups)
    sent = 0
    tweet_meta = tweet_meta or {}

    for i, (tweet_id, files) in enumerate(groups.items(), 1):
        if status:
            await status.update(f"📤 发送中 {i}/{total}…")

        meta = tweet_meta.get(tweet_id)
        tweet_text = meta.text if meta else ""
        cap = build_caption(files[0], tweet_text)

        def _do_send():
            tg_send.send_files(
                files,
                bot_token=s.telegram_bot_token,
                chat_id=str(dest_chat_id),
                caption=cap,
                api_base=s.telegram_api_base,
                proxy=s.proxy,
                max_upload_bytes=s.telegram_max_upload_bytes,
                split_oversized_video=s.telegram_split_oversized_video,
                compress_oversized_video=s.telegram_compress_oversized_video,
                ffmpeg_preset=s.telegram_ffmpeg_preset,
                rate_limit_seconds=s.telegram_rate_limit_seconds,
                send_retries=s.telegram_send_retries,
            )

        try:
            await _state.run_blocking(_do_send)
            sent += 1
            _state.db.tweet_upsert(
                tweet_id,
                author=(meta.author if meta else ""),
                tweet_text=tweet_text,
                url=(meta.url if meta else ""),
                files=[str(f) for f in files],
                sent_to=str(dest_chat_id),
            )
            if delete_after:
                for f in files:
                    try:
                        f.unlink(missing_ok=True)
                        cleanup_empty_dirs(f, s.download_dir)
                    except OSError:
                        pass
        except tg_send.TelegramFileTooLargeError as exc:
            log.warning("[{}] {} 文件过大: {}", tag, tweet_id, exc)
        except Exception as exc:
            log.warning("[{}] {} 发送失败: {}", tag, tweet_id, exc)

        await asyncio.sleep(s.telegram_rate_limit_seconds)
    return sent


# ---------------------------------------------------------------------------
# Per-URL processing (bot mode)
# ---------------------------------------------------------------------------


async def _process_url(
    bot: Bot,
    url: str,
    *,
    source_chat_id: int,
    reply_to: int,
) -> None:
    s = _state.settings
    dest = s.resolve_dest(source_chat_id)
    notify = source_chat_id if str(source_chat_id) != dest else None

    status: _Status | None = None
    if notify:
        status = _Status(bot, notify, reply_to)
        await status.send("⏳ 下载中…")

    _state.processed += 1
    _state.db.stat_inc("processed_total")

    try:
        with tempfile.TemporaryDirectory(prefix="xdl-") as tmpdir:
            tmp = Path(tmpdir)

            def _progress(event: str, path: Path) -> None:
                # 不要每个文件都 edit，loop_call 里做 throttling
                if status and event == "file":
                    asyncio.run_coroutine_threadsafe(
                        status.update(f"⬇️ 已下载 {path.name}"),
                        _state.loop,
                    )

            result = await _state.run_blocking(
                runner.run, url, s,
                use_archive=False,
                target_dir=tmp,
                retries=5,
                progress_cb=_progress,
            )

            if not result.new_files:
                if status:
                    await status.update("⚠️ 未找到媒体文件", force=True)
                _state.db.url_mark(url, "empty")
                return

            # 媒体 hash 去重（同一张图重复出现的场景）
            if s.media_hash_dedup:
                uniq: list[Path] = []
                dup = 0
                for f in result.new_files:
                    try:
                        h = file_md5(f)
                    except OSError:
                        uniq.append(f)
                        continue
                    if _state.db.media_hash_seen(h, tweet_id="", file_name=f.name):
                        dup += 1
                        f.unlink(missing_ok=True)
                    else:
                        uniq.append(f)
                if dup:
                    log.info("  [{}] 去重过滤 {} 个重复文件", url, dup)
                result.new_files = uniq
                if not result.new_files:
                    if status:
                        await status.update("ℹ️ 全部内容此前已发送过", force=True)
                    _state.db.url_mark(url, "duplicate")
                    return

            log.info("  [{}] 下载完成 {} 个文件，发送中…", url, len(result.new_files))
            if status:
                await status.update(f"📤 发送 {len(result.new_files)} 个文件…")

            groups = group_by_tweet(result.new_files)
            sent = await _send_groups(
                bot, groups,
                dest_chat_id=dest, status=status,
                tweet_meta=result.tweets,
                delete_after=False,  # bot mode 用 tmpdir 自动清理
                tag="bot",
            )
            _state.db.url_mark(url, "sent")
            if status:
                await status.update(f"✅ 已发送（{len(result.new_files)} 个文件）", force=True)
            log.info("  [{}] 发送成功 → {}（{} 条推文）", url, dest, sent)

    except tg_send.TelegramFileTooLargeError as exc:
        _state.db.url_mark(url, "too_large")
        _state.db.failed_add(url, str(source_chat_id), dest, f"文件过大: {exc}")
        if status:
            await status.update(f"⚠️ 文件过大: {exc}", force=True)
        log.warning("  [{}] 文件过大: {}", url, exc)
    except Exception as exc:
        _state.db.url_forget(url)  # 允许立即重试
        _state.db.failed_add(url, str(source_chat_id), dest, str(exc))
        if status:
            await status.update(f"❌ 失败: {exc}", force=True)
        log.warning("  [{}] 异常: {}", url, exc)


# ---------------------------------------------------------------------------
# Sync workflow
# ---------------------------------------------------------------------------


async def _do_sync(
    bot: Bot,
    *,
    source_chat_id: int,
    reply_to: int,
    target: str | None,
) -> None:
    if _state.sync_lock.locked():
        await bot.send_message(
            source_chat_id, "⏳ 同步已在进行中，请稍候…", reply_to_message_id=reply_to
        )
        return

    async with _state.sync_lock:
        s = _state.settings
        label = {"likes": "点赞", "bookmarks": "书签"}.get(target or "", "书签/点赞")
        status = _Status(bot, source_chat_id, reply_to)
        await status.send(f"⏳ 正在同步{label}…")

        try:
            all_urls = s.target_urls()
        except ValueError as exc:
            await status.update(f"❌ 配置错误: {exc}", force=True)
            return

        if target == "likes":
            urls = [u for u in all_urls if "/likes" in u]
        elif target == "bookmarks":
            urls = [u for u in all_urls if "bookmarks" in u]
        else:
            urls = all_urls

        if not urls:
            await status.update(f"⚠️ 未配置{label}目标", force=True)
            return

        all_new: list[Path] = []
        all_meta: dict[str, runner.TweetMeta] = {}
        try:
            for url in urls:
                log.info("[sync] → {}", url)
                await status.update(f"⏳ 拉取 {url} …")
                result = await _state.run_blocking(runner.run, url, s)
                all_new.extend(result.new_files)
                all_meta.update(result.tweets)
        except Exception as exc:
            _state.db.stat_set("last_sync_error", str(exc))
            await status.update(f"❌ 同步失败: {exc}", force=True)
            log.warning("[sync] 异常: {}", exc)
            return

        # 媒体 hash 去重
        if s.media_hash_dedup and all_new:
            uniq = []
            for f in all_new:
                try:
                    h = file_md5(f)
                except OSError:
                    uniq.append(f)
                    continue
                if not _state.db.media_hash_seen(h, file_name=f.name):
                    uniq.append(f)
                else:
                    f.unlink(missing_ok=True)
            all_new = uniq

        if not all_new:
            _state.db.stat_set("last_sync_at", str(time.time()))
            _state.db.stat_set("last_sync_count", "0")
            await status.update("✅ 同步完成，无新内容", force=True)
            return

        groups = group_by_tweet(all_new)
        dest = s.telegram_chat_id or str(source_chat_id)
        await status.update(
            f"📤 发现 {len(all_new)} 个新文件（{len(groups)} 条推文），发送中…",
            force=True,
        )

        sent = await _send_groups(
            bot, groups,
            dest_chat_id=dest, status=status,
            tweet_meta=all_meta,
            delete_after=s.delete_after_telegram,
            tag="sync",
        )
        _state.db.stat_set("last_sync_at", str(time.time()))
        _state.db.stat_set("last_sync_count", str(sent))
        await status.update(f"✅ 同步完成，共发送 {sent} 条推文", force=True)

        # 顺手做一次配额清理
        if s.download_dir_max_gb > 0:
            deleted, freed = await _state.run_blocking(
                disk.enforce_quota, s.download_dir, s.download_dir_max_gb
            )
            if deleted:
                log.info("[sync] 配额清理 {} 个文件，释放 {:.1f} MB",
                         deleted, freed / 1024 / 1024)


# ---------------------------------------------------------------------------
# /clear
# ---------------------------------------------------------------------------


async def _do_clear(bot: Bot, source_chat_id: int, reply_to: int) -> None:
    if _state.sync_lock.locked():
        await bot.send_message(
            source_chat_id, "⏳ 同步正在进行中，请等待完成后再清空", reply_to_message_id=reply_to
        )
        return

    async with _state.sync_lock:
        s = _state.settings
        download_dir = s.download_dir
        local = sorted(
            f for f in download_dir.rglob("*")
            if f.is_file() and f.suffix.lower() in MEDIA_SUFFIXES
        ) if download_dir.exists() else []
        dest = s.telegram_chat_id or str(source_chat_id)

        status = _Status(bot, source_chat_id, reply_to)
        if local:
            groups = group_by_tweet(local)
            await status.send(
                f"📤 发现 {len(local)} 个本地文件（{len(groups)} 条推文），发送后清空…"
            )
            sent = await _send_groups(
                bot, groups,
                dest_chat_id=dest, status=status,
                delete_after=True,
                tag="clear",
            )
            await status.update(f"✅ 已发送 {sent} 条推文并删除本地文件", force=True)
        else:
            await status.send("📭 本地无媒体文件")

        if s.archive_file.exists():
            s.archive_file.unlink()
            log.info("[clear] archive.db 已删除")
        await bot.send_message(source_chat_id, "🗑 下载记录已清空")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def cmd_start(message: Message) -> None:
    if not _allowed(message.chat.id):
        return
    await message.answer(
        "👋 发送推文/X 链接即可下载并转发。\n"
        "支持 X / Twitter / Pixiv / Instagram / Weibo / Reddit / Bilibili / YouTube\n\n"
        "/sync · /sync_likes · /sync_bookmarks · /status · /subscribe · /subs\n"
        "/search 关键词 · /retry_failed · /clear · /restart"
    )


async def cmd_sync(message: Message, command: CommandObject) -> None:
    if not _allowed(message.chat.id):
        return
    arg = (command.args or "").strip().lower()
    target = None
    if arg in ("likes", "bookmarks"):
        target = arg
    asyncio.create_task(_do_sync(
        message.bot, source_chat_id=message.chat.id,
        reply_to=message.message_id, target=target,
    ))


async def cmd_sync_likes(message: Message) -> None:
    if not _allowed(message.chat.id):
        return
    asyncio.create_task(_do_sync(
        message.bot, source_chat_id=message.chat.id,
        reply_to=message.message_id, target="likes",
    ))


async def cmd_sync_bookmarks(message: Message) -> None:
    if not _allowed(message.chat.id):
        return
    asyncio.create_task(_do_sync(
        message.bot, source_chat_id=message.chat.id,
        reply_to=message.message_id, target="bookmarks",
    ))


async def cmd_clear(message: Message) -> None:
    if not _allowed(message.chat.id):
        return
    asyncio.create_task(_do_clear(
        message.bot, source_chat_id=message.chat.id, reply_to=message.message_id,
    ))


async def cmd_status(message: Message) -> None:
    if not _allowed(message.chat.id):
        return
    s = _state.settings
    db = _state.db

    ok, msg = await _state.run_blocking(
        cookie_checker.check, s.cookies_file, s.proxy, str(s.gdl_config)
    )
    days = await _state.run_blocking(cookie_checker.days_until_expiry, s.cookies_file)
    cookie_line = f"{'✓' if ok else '✗'} {msg}"
    if days is not None and days > 0:
        cookie_line += f"（{days} 天后到期）"
    elif days is not None and days <= 0:
        cookie_line += "（已到期）"

    last_sync_at = db.stat_get("last_sync_at", "")
    last_sync_count = db.stat_get("last_sync_count", "0")
    last_sync_error = db.stat_get("last_sync_error", "")
    sync_line = "从未同步"
    if last_sync_at:
        ts = datetime.fromtimestamp(float(last_sync_at))
        sync_line = f"{ts.strftime('%m-%d %H:%M')}，+{last_sync_count} 条推文"

    uptime_h = (time.time() - _state.started_at) / 3600
    disk_bytes = await _state.run_blocking(disk.dir_size_bytes, s.download_dir)

    lines = [
        "📊 <b>x-dl 状态</b>",
        f"🍪 cookies: {html.escape(cookie_line)}",
        f"🔄 上次同步: {html.escape(sync_line)}",
        f"⏳ 同步中: {'是' if _state.sync_lock.locked() else '否'}",
        f"📥 处理总数: {html.escape(db.stat_get('processed_total', '0'))}（本次启动 {_state.processed}）",
        f"💾 下载目录: {disk_bytes / 1024 / 1024:.1f} MB"
        + (f" / 上限 {s.download_dir_max_gb} GB" if s.download_dir_max_gb > 0 else ""),
        f"📚 推文记录: {db.tweet_count()} 条 · 媒体 hash: {db.media_hash_count()}",
        f"📌 订阅: {len(db.sub_list())} 个 · 待重试: {db.failed_count()}",
        f"⏱ 运行时长: {uptime_h:.1f} 小时",
    ]
    if s.sync_interval_minutes > 0:
        lines.append(f"🕒 内置定时同步: 每 {s.sync_interval_minutes} 分钟")
    if last_sync_error:
        lines.append(f"❌ 上次错误: {html.escape(last_sync_error[:100])}")

    failed = db.failed_list(limit=5)
    if failed:
        lines.append("\n<b>最近失败:</b>")
        for row in failed:
            ts = datetime.fromtimestamp(row["created_at"]).strftime("%m-%d %H:%M")
            lines.append(f"  · {ts} {html.escape(row['url'][:60])} — {html.escape((row['error'] or '')[:60])}")

    await message.answer("\n".join(lines))


async def cmd_restart(message: Message) -> None:
    if not _allowed(message.chat.id):
        return
    await message.answer("🔄 正在重启整个项目…")
    log.info("收到 /restart 命令，准备 exec 重启 (pid={})", os.getpid())
    # 给消息一点时间送达 Telegram
    await asyncio.sleep(0.5)

    # 尽量优雅地落盘资源，execv 之后内核会回收 fd，但 sqlite WAL / log queue
    # 提前刷一下能减少丢日志的概率
    try:
        _state.db.close()
    except Exception as exc:
        log.debug("db close on restart: {}", exc)
    try:
        _state.executor.shutdown(wait=False, cancel_futures=True)
    except Exception as exc:
        log.debug("executor shutdown on restart: {}", exc)

    # 用当前的 Python 解释器 + 原始 argv 替换自己；execv 不返回
    python = sys.executable
    argv = [python, *sys.argv]
    log.info("execv {} {}", python, " ".join(sys.argv))
    os.execv(python, argv)


async def cmd_subscribe(message: Message, command: CommandObject) -> None:
    if not _allowed(message.chat.id):
        return
    arg = (command.args or "").strip()
    if not arg:
        await message.answer("用法: /subscribe @username")
        return
    handle = arg.split()[0].lstrip("@")
    if _state.db.sub_add(handle, str(message.chat.id)):
        await message.answer(f"✅ 已订阅 @{handle}")
        log.info("订阅 +1: @{} → chat {}", handle, message.chat.id)
    else:
        await message.answer(f"ℹ️ 已经订阅过 @{handle}")


async def cmd_unsubscribe(message: Message, command: CommandObject) -> None:
    if not _allowed(message.chat.id):
        return
    arg = (command.args or "").strip()
    if not arg:
        await message.answer("用法: /unsubscribe @username")
        return
    handle = arg.split()[0].lstrip("@")
    if _state.db.sub_remove(handle, str(message.chat.id)):
        await message.answer(f"✅ 已取消订阅 @{handle}")
    else:
        await message.answer(f"ℹ️ 你没有订阅 @{handle}")


async def cmd_subs(message: Message) -> None:
    if not _allowed(message.chat.id):
        return
    subs = _state.db.sub_list(str(message.chat.id))
    if not subs:
        await message.answer("当前没有订阅。用 /subscribe @username 添加。")
        return
    lines = ["📌 <b>订阅列表</b>"]
    for s in subs:
        last = "从未同步" if not s["last_synced_at"] else \
            datetime.fromtimestamp(s["last_synced_at"]).strftime("%m-%d %H:%M")
        lines.append(f"· @{html.escape(s['handle'])} — {last}")
    await message.answer("\n".join(lines))


async def cmd_search(message: Message, command: CommandObject) -> None:
    if not _allowed(message.chat.id):
        return
    q = (command.args or "").strip()
    if not q:
        await message.answer("用法: /search 关键词")
        return
    rows = await _state.run_blocking(_state.db.tweet_search, q, 10)
    if not rows:
        await message.answer("📭 没找到匹配的推文")
        return
    lines = [f"🔍 <b>搜索: {html.escape(q)}</b>"]
    for r in rows:
        ts = datetime.fromtimestamp(r["downloaded_at"]).strftime("%m-%d") if r["downloaded_at"] else ""
        snippet = (r["tweet_text"] or "")[:80].replace("\n", " ")
        url = r["url"] or f"https://x.com/i/status/{r['tweet_id']}"
        lines.append(f"· {ts} @{html.escape(r['author'] or '?')}: {html.escape(snippet)}\n  {url}")
    await message.answer("\n".join(lines), disable_web_page_preview=True)


async def cmd_retry_failed(message: Message) -> None:
    if not _allowed(message.chat.id):
        return
    rows = await _state.run_blocking(_state.db.failed_pop_all)
    if not rows:
        await message.answer("📭 没有待重试的链接")
        return
    await message.answer(f"🔁 重新提交 {len(rows)} 个失败链接…")
    for r in rows:
        chat = int(r["chat_id"]) if r["chat_id"] else message.chat.id
        asyncio.create_task(_process_url(
            message.bot, r["url"],
            source_chat_id=chat,
            reply_to=message.message_id,
        ))


async def cmd_disk_cleanup(message: Message) -> None:
    if not _allowed(message.chat.id):
        return
    s = _state.settings
    if s.download_dir_max_gb <= 0:
        await message.answer("ℹ️ DOWNLOAD_DIR_MAX_GB 未配置，跳过")
        return
    deleted, freed = await _state.run_blocking(
        disk.enforce_quota, s.download_dir, s.download_dir_max_gb
    )
    await message.answer(
        f"🧹 清理 {deleted} 个文件，释放 {freed / 1024 / 1024:.1f} MB"
    )


async def on_message(message: Message) -> None:
    if not _allowed(message.chat.id):
        log.info("忽略 chat_id={} 不在白名单", message.chat.id)
        return
    text = message.text or message.caption or ""
    if not text:
        return

    found = url_parser.find(text)
    if not found:
        return

    log.info("收到消息 chat_id={} from=@{} 找到 {} 个链接",
             message.chat.id, message.from_user.username if message.from_user else "?",
             len(found))

    ttl = _state.settings.url_dedup_ttl_days * 86400
    new_urls: list[tuple[str, str]] = []
    for platform, url in found:
        if _state.db.url_seen(url, ttl):
            log.info("  [{}] 命中去重窗口，跳过", url)
            continue
        new_urls.append((platform, url))

    if not new_urls:
        return

    for platform, url in new_urls:
        asyncio.create_task(_process_url(
            message.bot, url,
            source_chat_id=message.chat.id,
            reply_to=message.message_id,
        ))


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------


async def _scheduler_loop(bot: Bot) -> None:
    """内置定时同步：替代 systemd timer。"""
    s = _state.settings
    if s.sync_interval_minutes <= 0:
        log.info("内置定时同步未启用（SYNC_INTERVAL_MINUTES=0）")
        return
    await asyncio.sleep(120)  # 启动后稍等
    interval = s.sync_interval_minutes * 60
    log.info("内置定时同步已启动，每 {} 分钟一次", s.sync_interval_minutes)
    while True:
        try:
            target_chat = (
                int(s.telegram_chat_id)
                if s.telegram_chat_id and s.telegram_chat_id.lstrip("-").isdigit()
                else None
            )
            if target_chat is not None:
                await _do_sync(bot, source_chat_id=target_chat, reply_to=0, target=None)
            else:
                log.warning("[scheduler] TELEGRAM_CHAT_ID 未配置或非数字，跳过定时同步")
        except Exception as exc:
            log.warning("[scheduler] 同步异常: {}", exc)
        await asyncio.sleep(interval)


async def _subscription_loop(bot: Bot) -> None:
    """订阅扫描：拉取每个订阅用户的 media 时间线。"""
    s = _state.settings
    if s.subscription_interval_minutes <= 0:
        return
    await asyncio.sleep(60)
    interval = s.subscription_interval_minutes * 60
    log.info("订阅扫描已启动，每 {} 分钟一次", s.subscription_interval_minutes)

    while True:
        try:
            subs = _state.db.sub_list()
            if subs:
                log.info("[subs] 扫描 {} 个订阅", len(subs))
            for sub in subs:
                handle = sub["handle"]
                chat = sub["chat_id"]
                feed_url = f"https://x.com/{handle}/media"
                try:
                    result = await _state.run_blocking(runner.run, feed_url, s)
                except Exception as exc:
                    log.warning("[subs] @{} 拉取失败: {}", handle, exc)
                    continue

                if not result.new_files:
                    _state.db.sub_mark_synced(sub["id"])
                    continue

                if s.media_hash_dedup:
                    uniq = []
                    for f in result.new_files:
                        try:
                            h = file_md5(f)
                        except OSError:
                            uniq.append(f); continue
                        if not _state.db.media_hash_seen(h, file_name=f.name):
                            uniq.append(f)
                        else:
                            f.unlink(missing_ok=True)
                    result.new_files = uniq
                if not result.new_files:
                    _state.db.sub_mark_synced(sub["id"])
                    continue

                groups = group_by_tweet(result.new_files)
                dest = s.resolve_dest(chat)
                log.info("[subs] @{} 新增 {} 文件 → chat {}",
                         handle, len(result.new_files), dest)
                await _send_groups(
                    bot, groups,
                    dest_chat_id=dest, status=None,
                    tweet_meta=result.tweets,
                    delete_after=s.delete_after_telegram,
                    tag="subs",
                )
                _state.db.sub_mark_synced(sub["id"])
        except Exception as exc:
            log.warning("[subs] loop 异常: {}", exc)
        await asyncio.sleep(interval)


async def _cookies_watcher(bot: Bot) -> None:
    s = _state.settings
    await asyncio.sleep(60)
    warn_days = 7
    while True:
        try:
            targets: list[str] = list(s.bot_allowed_chat_ids) or (
                [s.telegram_chat_id] if s.telegram_chat_id else []
            )
            if targets:
                days = await _state.run_blocking(
                    cookie_checker.days_until_expiry, s.cookies_file
                )
                if days is not None and days <= 0:
                    for t in targets:
                        try:
                            await bot.send_message(t, "🔴 cookies 已到期，请立即更新！")
                        except Exception:
                            pass
                elif days is not None and days < warn_days:
                    for t in targets:
                        try:
                            await bot.send_message(t, f"⚠️ cookies 将在 {days} 天后到期")
                        except Exception:
                            pass
        except Exception as exc:
            log.debug("cookies watcher: {}", exc)
        await asyncio.sleep(12 * 3600)


async def _url_prune_loop() -> None:
    s = _state.settings
    await asyncio.sleep(3600)
    while True:
        try:
            n = _state.db.url_prune(s.url_dedup_ttl_days * 86400 * 4)
            if n:
                log.debug("[url_prune] 清理 {} 条过期记录", n)
        except Exception as exc:
            log.debug("url prune: {}", exc)
        await asyncio.sleep(6 * 3600)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def _make_bot(settings: Settings) -> Bot:
    session = AiohttpSession(proxy=settings.proxy or None)
    api_base = settings.telegram_api_base.rstrip("/")
    if api_base and api_base != "https://api.telegram.org":
        # 自建 local bot-api server（上传可达 2 GB）
        from aiogram.client.telegram import TelegramAPIServer
        session.api = TelegramAPIServer.from_base(api_base)
    return Bot(
        token=settings.telegram_bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def _register(dp: Dispatcher) -> None:
    dp.message.register(cmd_start,           Command("start"))
    dp.message.register(cmd_sync_likes,      Command("sync_likes"))
    dp.message.register(cmd_sync_bookmarks,  Command("sync_bookmarks"))
    dp.message.register(cmd_sync,            Command("sync"))
    dp.message.register(cmd_clear,           Command("clear"))
    dp.message.register(cmd_status,          Command("status"))
    dp.message.register(cmd_restart,         Command("restart"))
    dp.message.register(cmd_subscribe,       Command("subscribe"))
    dp.message.register(cmd_unsubscribe,     Command("unsubscribe"))
    dp.message.register(cmd_subs,            Command("subs"))
    dp.message.register(cmd_search,          Command("search"))
    dp.message.register(cmd_retry_failed,    Command("retry_failed"))
    dp.message.register(cmd_disk_cleanup,    Command("disk_cleanup"))
    dp.message.register(on_message, F.text | F.caption)


async def _set_commands(bot: Bot) -> None:
    cmds = [
        BotCommand(command="sync", description="同步所有目标（书签 + 点赞）"),
        BotCommand(command="sync_likes", description="只同步点赞"),
        BotCommand(command="sync_bookmarks", description="只同步书签"),
        BotCommand(command="subscribe", description="订阅 @用户名"),
        BotCommand(command="unsubscribe", description="取消订阅"),
        BotCommand(command="subs", description="查看订阅列表"),
        BotCommand(command="search", description="检索本地推文"),
        BotCommand(command="retry_failed", description="重试失败链接"),
        BotCommand(command="disk_cleanup", description="手动清理磁盘"),
        BotCommand(command="clear", description="清空 archive 并转发本地文件"),
        BotCommand(command="status", description="查看状态"),
        BotCommand(command="restart", description="重启 Bot"),
    ]
    try:
        await bot.set_my_commands(cmds)
    except Exception as exc:
        log.warning("set_my_commands 失败: {}", exc)


async def _async_main(settings: Settings) -> None:
    global _state
    _state = _State(settings)
    _state.loop = asyncio.get_running_loop()

    bot = _make_bot(settings)
    dp = Dispatcher()
    _register(dp)

    me = await bot.get_me()
    log.info("已连接: @{} ({})", me.username, me.first_name)
    log.info("白名单 chat_id: {}",
             ", ".join(settings.bot_allowed_chat_ids) or "无（响应所有人）")

    await _set_commands(bot)

    # 后台任务
    asyncio.create_task(_cookies_watcher(bot))
    asyncio.create_task(_scheduler_loop(bot))
    asyncio.create_task(_subscription_loop(bot))
    asyncio.create_task(_url_prune_loop())

    if settings.webhook_url:
        await _run_webhook(bot, dp, settings)
    else:
        log.info("Bot 已启动（long polling），并发线程 {}", settings.bot_max_workers)
        await dp.start_polling(bot, allowed_updates=["message"])


async def _run_webhook(bot: Bot, dp: Dispatcher, s: Settings) -> None:
    log.info("Bot 已启动（webhook @ {}{}）", s.webhook_url, s.webhook_path)
    await bot.set_webhook(
        url=s.webhook_url.rstrip("/") + s.webhook_path,
        secret_token=s.webhook_secret or None,
        allowed_updates=["message"],
        drop_pending_updates=False,
    )
    app = web.Application()
    SimpleRequestHandler(
        dispatcher=dp, bot=bot, secret_token=s.webhook_secret or None,
    ).register(app, path=s.webhook_path)
    setup_application(app, dp, bot=bot)

    runner_ = web.AppRunner(app)
    await runner_.setup()
    site = web.TCPSite(runner_, s.webhook_listen, s.webhook_port)
    await site.start()

    # 永久等待
    while True:
        await asyncio.sleep(3600)


def run() -> None:
    settings = load_settings()
    logging_setup.setup(settings.log_file, settings.log_level)

    if not settings.telegram_bot_token:
        raise SystemExit("错误: TELEGRAM_BOT_TOKEN 未在 .env 中配置")
    if not Path(settings.cookies_file).exists():
        raise SystemExit(f"错误: cookies 文件不存在: {settings.cookies_file}")

    try:
        asyncio.run(_async_main(settings))
    except (KeyboardInterrupt, SystemExit):
        log.info("退出")
