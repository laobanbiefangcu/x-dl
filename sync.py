#!/usr/bin/env python3
"""x-dl: 用 gallery-dl 同步 X (Twitter) 书签/点赞的媒体文件."""
from __future__ import annotations

import re
import time
from pathlib import Path

from xdl.config import load_settings
from xdl import runner, telegram, cookies as cookie_checker

# 匹配 gallery-dl 生成的文件名: {author}_{tweet_id}_{num}.{ext}
_FNAME_RE = re.compile(r"^([^_]+)_(\d{10,})_\d+\.")


def _caption(path: Path) -> str:
    m = _FNAME_RE.match(path.name)
    if m:
        author, tweet_id = m.group(1), m.group(2)
        return f"@{author}\nhttps://x.com/{author}/status/{tweet_id}"
    return path.stem


def _group_by_tweet(files: list[Path]) -> dict[str, list[Path]]:
    """同一条推文的多个媒体文件归为一组，作为 media group 发送."""
    groups: dict[str, list[Path]] = {}
    for f in files:
        m = _FNAME_RE.match(f.name)
        key = m.group(2) if m else f.stem
        groups.setdefault(key, []).append(f)
    return groups


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
        raise SystemExit("cookies 无效，终止同步。")

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

    groups = _group_by_tweet(all_new)
    print(f"\n开始发送到 Telegram（共 {len(groups)} 条推文）...")

    for tweet_id, files in groups.items():
        caption = _caption(files[0])
        try:
            telegram.send_files(
                files,
                bot_token=settings.telegram_bot_token,
                chat_id=settings.telegram_chat_id,
                caption=caption,
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
        except telegram.TelegramFileTooLargeError as exc:
            print(f"  [skip] {tweet_id}: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {tweet_id} 发送失败: {exc}")

        time.sleep(settings.telegram_rate_limit_seconds)

    print("\n全部完成。")


if __name__ == "__main__":
    main()
