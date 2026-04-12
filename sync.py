#!/usr/bin/env python3
"""x-dl: 用 gallery-dl 同步 X (Twitter) 书签/点赞的媒体文件."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

from xdl.config import load_settings
from xdl import runner, telegram, cookies as cookie_checker
from xdl.utils import caption, group_by_tweet, cleanup_empty_dirs


def _notify_tg(settings, text: str) -> None:
    """向 Telegram 发送一条纯文本通知，失败静默。"""
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
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    settings = load_settings()

    print("🍪 检测 cookies 有效性…")
    ok, msg = cookie_checker.check(
        settings.cookies_file, settings.proxy, str(settings.gdl_config)
    )
    if ok:
        print(f"   ✓ {msg}")
    else:
        print(f"   ✗ {msg}")
        _notify_tg(settings, f"⚠️ x-dl sync: cookies 无效，已停止同步\n{msg}")
        sys.exit(0)  # 软退出：cookies 失效是预期状态，不让 systemd 显示 Failed

    urls = settings.target_urls()

    print(f"同步目标: {', '.join(urls)}")

    all_new: list[Path] = []
    for url in urls:
        print(f"\n→ {url}")
        new_files = runner.run(url, settings)
        if new_files:
            print(f"  新增 {len(new_files)} 个文件:")
            for f in new_files:
                print(f"  - {f.relative_to(settings.download_dir)}")
        else:
            print("  没有新内容。")
        all_new.extend(new_files)

    if not all_new:
        print("\n完成，无新文件。")
        return

    print(f"\n本次共下载 {len(all_new)} 个文件。")

    if not settings.telegram_enabled:
        return

    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        print("[warn] TELEGRAM_ENABLED=true 但未配置 BOT_TOKEN 或 CHAT_ID，跳过发送。")
        return

    groups = group_by_tweet(all_new)
    print(f"\n开始发送到 Telegram（共 {len(groups)} 条推文）...")

    for tweet_id, files in groups.items():
        cap = caption(files[0])
        try:
            telegram.send_files(
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
            print(f"  ✓ {tweet_id} ({len(files)} 个文件)")
            if settings.delete_after_telegram:
                for f in files:
                    f.unlink(missing_ok=True)
                    cleanup_empty_dirs(f, settings.download_dir)
        except telegram.TelegramFileTooLargeError as exc:
            print(f"  [skip] {tweet_id}: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {tweet_id} 发送失败: {exc}")

        time.sleep(settings.telegram_rate_limit_seconds)

    print("\n全部完成。")


if __name__ == "__main__":
    main()
