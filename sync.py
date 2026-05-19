#!/usr/bin/env python3
"""x-dl: 用 gallery-dl 同步 X (Twitter) 书签/点赞的媒体文件。

定时执行入口（也可手动跑）。Bot 模式下内置了 scheduler，可以不用这个脚本。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

from xdl import cookies as cookie_checker
from xdl import db as db_module
from xdl import disk
from xdl import logging_setup
from xdl import runner
from xdl import telegram as tg_send
from xdl.config import load_settings
from xdl.utils import caption, cleanup_empty_dirs, file_md5, group_by_tweet


def _notify_tg(settings, text: str) -> None:
    if not settings.telegram_enabled:
        return
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return
    try:
        requests.post(
            f"{settings.telegram_api_base.rstrip('/')}/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": settings.telegram_chat_id, "text": text},
            timeout=10,
        )
    except Exception:
        pass


def main() -> None:
    settings = load_settings()
    logging_setup.setup(settings.log_file, settings.log_level)
    db = db_module.get(settings.db_file)

    print("🍪 检测 cookies 有效性…")
    ok, msg = cookie_checker.check(
        settings.cookies_file, settings.proxy, str(settings.gdl_config)
    )
    if ok:
        print(f"   ✓ {msg}")
    else:
        print(f"   ✗ {msg}")
        _notify_tg(settings, f"⚠️ x-dl sync: cookies 无效，已停止同步\n{msg}")
        sys.exit(0)

    urls = settings.target_urls()
    print(f"同步目标: {', '.join(urls)}")

    all_new: list[Path] = []
    all_meta: dict[str, runner.TweetMeta] = {}
    for url in urls:
        print(f"\n→ {url}")
        try:
            result = runner.run(url, settings)
        except Exception as exc:
            print(f"  ✗ 拉取失败: {exc}")
            db.stat_set("last_sync_error", str(exc))
            continue
        if result.new_files:
            print(f"  新增 {len(result.new_files)} 个文件:")
            for f in result.new_files:
                print(f"  - {f.relative_to(settings.download_dir)}")
        else:
            print("  没有新内容。")
        all_new.extend(result.new_files)
        all_meta.update(result.tweets)

    # 媒体 hash 去重
    if settings.media_hash_dedup and all_new:
        uniq: list[Path] = []
        dup = 0
        for f in all_new:
            try:
                h = file_md5(f)
            except OSError:
                uniq.append(f); continue
            if db.media_hash_seen(h, file_name=f.name):
                dup += 1
                f.unlink(missing_ok=True)
            else:
                uniq.append(f)
        if dup:
            print(f"  去重过滤 {dup} 个重复文件")
        all_new = uniq

    if not all_new:
        print("\n完成，无新文件。")
        db.stat_set("last_sync_at", str(time.time()))
        db.stat_set("last_sync_count", "0")
        return

    print(f"\n本次共下载 {len(all_new)} 个文件。")

    if not settings.telegram_enabled:
        return
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        print("[warn] TELEGRAM_ENABLED=true 但未配置 BOT_TOKEN 或 CHAT_ID，跳过发送。")
        return

    groups = group_by_tweet(all_new)
    print(f"\n开始发送到 Telegram（共 {len(groups)} 条推文）...")
    sent_count = 0
    for tweet_id, files in groups.items():
        meta = all_meta.get(tweet_id)
        cap = caption(files[0], meta.text if meta else "")
        try:
            tg_send.send_files(
                files,
                bot_token=settings.telegram_bot_token,
                chat_id=settings.telegram_chat_id,
                caption=cap,
                api_base=settings.telegram_api_base,
                proxy=settings.proxy,
                max_upload_bytes=settings.telegram_max_upload_bytes,
                split_oversized_video=settings.telegram_split_oversized_video,
                compress_oversized_video=settings.telegram_compress_oversized_video,
                ffmpeg_preset=settings.telegram_ffmpeg_preset,
                rate_limit_seconds=settings.telegram_rate_limit_seconds,
                send_retries=settings.telegram_send_retries,
            )
            sent_count += 1
            print(f"  ✓ {tweet_id} ({len(files)} 个文件)")
            db.tweet_upsert(
                tweet_id,
                author=meta.author if meta else "",
                tweet_text=meta.text if meta else "",
                url=meta.url if meta else "",
                files=[str(f) for f in files],
                sent_to=settings.telegram_chat_id,
            )
            if settings.delete_after_telegram:
                for f in files:
                    f.unlink(missing_ok=True)
                    cleanup_empty_dirs(f, settings.download_dir)
        except tg_send.TelegramFileTooLargeError as exc:
            print(f"  [skip] {tweet_id}: {exc}")
            db.failed_add(f"tweet:{tweet_id}", None, settings.telegram_chat_id, f"文件过大: {exc}")
        except Exception as exc:
            print(f"  ✗ {tweet_id} 发送失败: {exc}")
            db.failed_add(f"tweet:{tweet_id}", None, settings.telegram_chat_id, str(exc))

        time.sleep(settings.telegram_rate_limit_seconds)

    db.stat_set("last_sync_at", str(time.time()))
    db.stat_set("last_sync_count", str(sent_count))
    db.stat_set("last_sync_error", "")

    # 磁盘配额
    if settings.download_dir_max_gb > 0:
        deleted, freed = disk.enforce_quota(settings.download_dir, settings.download_dir_max_gb)
        if deleted:
            print(f"\n🧹 配额清理 {deleted} 个文件，释放 {freed / 1024 / 1024:.1f} MB")

    print("\n全部完成。")


if __name__ == "__main__":
    main()
