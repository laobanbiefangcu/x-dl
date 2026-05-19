from __future__ import annotations

import json
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


def _int(key: str, default: int) -> int:
    v = os.getenv(key)
    if v is None or not v.strip():
        return default
    try:
        return int(v.strip())
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    v = os.getenv(key)
    if v is None or not v.strip():
        return default
    try:
        return float(v.strip())
    except ValueError:
        return default


def _json(key: str, default):
    v = os.getenv(key)
    if not v or not v.strip():
        return default
    try:
        return json.loads(v)
    except json.JSONDecodeError:
        return default


@dataclass(slots=True)
class Settings:
    # 基础
    cookies_file: str
    x_handle: str
    sync_targets: list[str]
    download_dir: Path
    proxy: str
    archive_file: Path
    gdl_config: Path
    data_dir: Path
    db_file: Path
    log_file: Path

    # Telegram
    telegram_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str
    bot_allowed_chat_ids: list[str]
    telegram_api_base: str
    telegram_rate_limit_seconds: float
    telegram_send_retries: int
    telegram_max_upload_bytes: int
    telegram_split_oversized_video: bool
    telegram_compress_oversized_video: bool
    telegram_ffmpeg_preset: str
    delete_after_telegram: bool

    # Bot 并发 / scheduler
    bot_max_workers: int
    sync_interval_minutes: int
    subscription_interval_minutes: int

    # Webhook
    webhook_url: str
    webhook_listen: str
    webhook_port: int
    webhook_path: str
    webhook_secret: str

    # 路由：source chat_id -> dest chat_id
    routes: dict[str, str]

    # 去重 / 配额
    url_dedup_ttl_days: float
    download_dir_max_gb: float
    media_hash_dedup: bool

    # 日志
    log_level: str

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

    def resolve_dest(self, source_chat_id: str | int) -> str:
        """根据来源 chat_id 找到目标频道。"""
        key = str(source_chat_id)
        if key in self.routes:
            return self.routes[key]
        return self.telegram_chat_id or key


def load_settings() -> Settings:
    data_dir = ROOT_DIR / "data"
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
        archive_file=data_dir / "archive.db",
        gdl_config=ROOT_DIR / "gdl_config.json",
        data_dir=data_dir,
        db_file=data_dir / "xdl.db",
        log_file=ROOT_DIR / os.getenv("LOG_FILE", "data/bot.log").strip(),

        telegram_enabled=_bool("TELEGRAM_ENABLED"),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        bot_allowed_chat_ids=[
            cid.strip() for cid in os.getenv("BOT_ALLOWED_CHAT_IDS", "").split(",")
            if cid.strip()
        ],
        telegram_api_base=os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").strip(),
        telegram_rate_limit_seconds=_float("TELEGRAM_RATE_LIMIT_SECONDS", 1.5),
        telegram_send_retries=_int("TELEGRAM_SEND_RETRIES", 3),
        telegram_max_upload_bytes=_int("TELEGRAM_MAX_UPLOAD_BYTES", 50 * 1024 * 1024),
        telegram_split_oversized_video=_bool("TELEGRAM_SPLIT_OVERSIZED_VIDEO", default=True),
        telegram_compress_oversized_video=_bool("TELEGRAM_COMPRESS_OVERSIZED_VIDEO", default=True),
        telegram_ffmpeg_preset=os.getenv("TELEGRAM_FFMPEG_PRESET", "veryfast").strip() or "veryfast",
        delete_after_telegram=_bool("DELETE_LOCAL_AFTER_TELEGRAM"),

        bot_max_workers=_int("BOT_MAX_WORKERS", 4),
        sync_interval_minutes=_int("SYNC_INTERVAL_MINUTES", 0),  # 0 = 关闭内置调度
        subscription_interval_minutes=_int("SUBSCRIPTION_INTERVAL_MINUTES", 60),

        webhook_url=os.getenv("WEBHOOK_URL", "").strip(),
        webhook_listen=os.getenv("WEBHOOK_LISTEN", "0.0.0.0").strip(),
        webhook_port=_int("WEBHOOK_PORT", 8443),
        webhook_path=os.getenv("WEBHOOK_PATH", "/tg/webhook").strip(),
        webhook_secret=os.getenv("WEBHOOK_SECRET", "").strip(),

        routes={str(k): str(v) for k, v in (_json("ROUTES_JSON", {}) or {}).items()},

        url_dedup_ttl_days=_float("URL_DEDUP_TTL_DAYS", 7.0),
        download_dir_max_gb=_float("DOWNLOAD_DIR_MAX_GB", 0),  # 0 = 不清理
        media_hash_dedup=_bool("MEDIA_HASH_DEDUP", default=True),

        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )
