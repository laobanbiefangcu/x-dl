from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def _bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    cookies_file: str
    x_handle: str
    sync_targets: list[str]
    download_dir: Path
    proxy: str
    archive_file: Path
    gdl_config: Path

    telegram_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str        # sync 推送目标；bot 模式用 bot_allowed_chat_ids
    bot_allowed_chat_ids: list[str]  # 空列表 = 所有人；否则只响应这些 chat_id
    telegram_api_base: str
    telegram_rate_limit_seconds: float
    telegram_send_retries: int
    telegram_max_upload_bytes: int
    telegram_split_oversized_video: bool
    telegram_compress_oversized_video: bool
    telegram_ffmpeg_preset: str
    delete_after_telegram: bool

    def target_urls(self) -> list[str]:
        urls = []
        for t in self.sync_targets:
            if t.startswith("http"):
                urls.append(t)
            elif t == "bookmarks":
                urls.append("https://x.com/i/bookmarks")
            elif t == "likes":
                if not self.x_handle:
                    raise ValueError("X_HANDLE is required for likes sync")
                urls.append(f"https://x.com/{self.x_handle}/likes")
            else:
                raise ValueError(f"Unknown sync target: {t!r}. Use bookmarks, likes, or a URL.")
        return urls


def load_settings() -> Settings:
    return Settings(
        cookies_file=os.getenv("COOKIES_FILE", "cookies.txt").strip(),
        x_handle=os.getenv("X_HANDLE", "").strip().lstrip("@"),
        sync_targets=[
            t.strip().lower()
            for t in os.getenv("SYNC_TARGETS", "bookmarks").split(",")
            if t.strip()
        ],
        download_dir=ROOT_DIR / os.getenv("DOWNLOAD_DIR", "downloads").strip(),
        proxy=os.getenv("PROXY", "").strip(),
        archive_file=ROOT_DIR / "data" / "archive.db",
        gdl_config=ROOT_DIR / "gdl_config.json",

        telegram_enabled=_bool("TELEGRAM_ENABLED"),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        bot_allowed_chat_ids=[
            cid.strip() for cid in os.getenv("BOT_ALLOWED_CHAT_IDS", "").split(",")
            if cid.strip()
        ],
        telegram_api_base=os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").strip(),
        telegram_rate_limit_seconds=float(os.getenv("TELEGRAM_RATE_LIMIT_SECONDS", "1.5")),
        telegram_send_retries=int(os.getenv("TELEGRAM_SEND_RETRIES", "3")),
        telegram_max_upload_bytes=int(os.getenv("TELEGRAM_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024))),
        telegram_split_oversized_video=_bool("TELEGRAM_SPLIT_OVERSIZED_VIDEO", default=True),
        telegram_compress_oversized_video=_bool("TELEGRAM_COMPRESS_OVERSIZED_VIDEO", default=True),
        telegram_ffmpeg_preset=os.getenv("TELEGRAM_FFMPEG_PRESET", "veryfast").strip() or "veryfast",
        delete_after_telegram=_bool("DELETE_LOCAL_AFTER_TELEGRAM"),
    )
