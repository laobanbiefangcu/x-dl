"""Telegram delivery helpers — 支持超大视频自动分割/压缩."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

import requests

from .utils import make_proxies

ProgressCallback = Callable[[int, int], None]  # (sent, total)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
ANIMATION_SUFFIXES = {".gif"}
_GROUPABLE_SUFFIXES = IMAGE_SUFFIXES | VIDEO_SUFFIXES  # 预计算，sendMediaGroup 支持的类型
DEFAULT_MAX_BYTES = 50 * 1024 * 1024


class TelegramFileTooLargeError(RuntimeError):
    pass


class TelegramRateLimitError(RuntimeError):
    def __init__(self, retry_after: int, desc: str = "") -> None:
        super().__init__(f"Telegram 429: {desc}")
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_files(
    files: list[Path],
    *,
    bot_token: str,
    chat_id: str,
    caption: str = "",
    api_base: str = "https://api.telegram.org",
    proxy: str = "",
    max_upload_bytes: int = DEFAULT_MAX_BYTES,
    split_oversized_video: bool = True,
    compress_oversized_video: bool = True,
    ffmpeg_preset: str = "veryfast",
    rate_limit_seconds: float = 1.5,
    send_retries: int = 3,
    progress_cb: ProgressCallback | None = None,
) -> None:
    if not bot_token or not chat_id:
        raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.")
    if not files:
        return

    kwargs = dict(bot_token=bot_token, chat_id=chat_id, api_base=api_base, proxy=proxy)

    with tempfile.TemporaryDirectory() as workdir:
        prepared = _prepare(
            files,
            workdir=Path(workdir),
            max_upload_bytes=max_upload_bytes,
            split_oversized_video=split_oversized_video,
            compress_oversized_video=compress_oversized_video,
            ffmpeg_preset=ffmpeg_preset,
        )
        total = len(prepared)
        sent = 0

        def _tick(n: int) -> None:
            nonlocal sent
            sent += n
            if progress_cb:
                try:
                    progress_cb(sent, total)
                except Exception:
                    pass

        first = True
        for chunk in [prepared[i:i + 10] for i in range(0, len(prepared), 10)]:
            if _can_group(chunk):
                if not first and rate_limit_seconds > 0:
                    time.sleep(rate_limit_seconds)
                _retry(_send_group, send_retries, chunk,
                       caption=caption if first else "", **kwargs)
                _tick(len(chunk))
                first = False
            else:
                for path in chunk:
                    if not first and rate_limit_seconds > 0:
                        time.sleep(rate_limit_seconds)
                    _retry(_send_one, send_retries, path,
                           caption=caption if first else "", **kwargs)
                    _tick(1)
                    first = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _retry(fn, retries: int, *args, **kwargs) -> None:
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            fn(*args, **kwargs)
            return
        except TelegramRateLimitError as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(exc.retry_after + 1)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < retries:
                time.sleep(attempt * 2)
    raise last_exc  # type: ignore[misc]


def _send_one(path: Path, *, bot_token: str, chat_id: str, caption: str,
              api_base: str, proxy: str) -> None:
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        method, field = "sendPhoto", "photo"
    elif suffix in VIDEO_SUFFIXES:
        method, field = "sendVideo", "video"
    elif suffix in ANIMATION_SUFFIXES:
        method, field = "sendAnimation", "animation"
    else:
        method, field = "sendDocument", "document"

    url = f"{api_base.rstrip('/')}/bot{bot_token}/{method}"
    data: dict = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption

    with path.open("rb") as fh:
        resp = requests.post(url, data=data, files={field: (path.name, fh)},
                             proxies=make_proxies(proxy), timeout=120)
    _check(resp)


def _send_group(files: list[Path], *, bot_token: str, chat_id: str, caption: str,
                api_base: str, proxy: str) -> None:
    url = f"{api_base.rstrip('/')}/bot{bot_token}/sendMediaGroup"
    media = []
    attach: dict = {}
    handles = []
    try:
        for i, path in enumerate(files):
            suffix = path.suffix.lower()
            mtype = "photo" if suffix in IMAGE_SUFFIXES else "video"
            item: dict = {"type": mtype, "media": f"attach://f{i}"}
            if i == 0 and caption:
                item["caption"] = caption
            media.append(item)
            fh = path.open("rb")
            handles.append(fh)
            attach[f"f{i}"] = (path.name, fh)

        resp = requests.post(
            url,
            data={"chat_id": chat_id, "media": json.dumps(media, ensure_ascii=True)},
            files=attach,
            proxies=make_proxies(proxy),
            timeout=180,
        )
    finally:
        for fh in handles:
            fh.close()
    _check(resp)


def _can_group(files: list[Path]) -> bool:
    if not (2 <= len(files) <= 10):
        return False
    return all(f.suffix.lower() in _GROUPABLE_SUFFIXES for f in files)


def _check(resp: requests.Response) -> None:
    try:
        payload = resp.json()
    except Exception:
        resp.raise_for_status()
        return
    if not payload.get("ok"):
        desc = payload.get("description", "")
        retry_after = (payload.get("parameters") or {}).get("retry_after")
        if resp.status_code == 429 and retry_after:
            raise TelegramRateLimitError(int(retry_after), desc)
        raise RuntimeError(f"Telegram {resp.status_code}: {desc}")


# ---------------------------------------------------------------------------
# Oversized video: split or compress
# ---------------------------------------------------------------------------

def _prepare(
    files: list[Path],
    *,
    workdir: Path,
    max_upload_bytes: int,
    split_oversized_video: bool,
    compress_oversized_video: bool,
    ffmpeg_preset: str,
) -> list[Path]:
    prepared: list[Path] = []

    for path in files:
        size = path.stat().st_size
        if size <= max_upload_bytes:
            prepared.append(path)
            continue
        if path.suffix.lower() not in VIDEO_SUFFIXES:
            raise TelegramFileTooLargeError(
                f"{path.name} ({size} bytes) exceeds Telegram limit ({max_upload_bytes} bytes)."
            )
        if split_oversized_video:
            prepared.extend(_split(path, max_upload_bytes, workdir))
        elif compress_oversized_video:
            prepared.append(_compress(path, max_upload_bytes, ffmpeg_preset, workdir))
        else:
            raise TelegramFileTooLargeError(
                f"{path.name} ({size} bytes) exceeds Telegram limit ({max_upload_bytes} bytes)."
            )

    return prepared


def _require_ffmpeg() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise TelegramFileTooLargeError("ffmpeg/ffprobe not found — required for oversized video handling.")


def _duration(path: Path) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return float(result.stdout.strip() or "0")


def _compress(path: Path, limit: int, preset: str, workdir: Path) -> Path:
    _require_ffmpeg()
    dur = _duration(path)
    if dur <= 0:
        raise TelegramFileTooLargeError(f"Cannot determine duration of {path.name}.")
    audio_br = 64_000
    total_br = int((limit * 8 * 0.92) / dur)
    video_br = max(120_000, total_br - audio_br)
    out = workdir / f"{path.stem}.tg.mp4"
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", str(path),
         "-movflags", "+faststart",
         "-c:v", "libx264", "-preset", preset,
         "-b:v", str(video_br), "-maxrate", str(int(video_br * 1.3)),
         "-bufsize", str(max(video_br * 2, 240_000)),
         "-c:a", "aac", "-b:a", str(audio_br), str(out)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        detail = proc.stderr[-300:] if proc.stderr else "no output"
        raise TelegramFileTooLargeError(f"Compression failed for {path.name}: {detail}")
    if out.stat().st_size > limit:
        raise TelegramFileTooLargeError(
            f"Compressed video still exceeds limit: {out.name} = {out.stat().st_size} bytes."
        )
    return out


def _split(path: Path, limit: int, workdir: Path, *, _depth: int = 0) -> list[Path]:
    if path.stat().st_size <= limit:
        return [path]
    if _depth >= 4:
        raise TelegramFileTooLargeError(f"Cannot split {path.name} below limit.")
    _require_ffmpeg()

    dur = _duration(path)
    if dur <= 0:
        raise TelegramFileTooLargeError(f"Cannot determine duration of {path.name}.")
    bps = max(path.stat().st_size / dur, 1)
    seg_secs = max(5, int((limit * 0.9) / bps))

    stem = path.stem.replace(".tg", "")
    pattern = workdir / f"{stem}.pt%03d{path.suffix}"
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", str(path), "-map", "0", "-c", "copy",
         "-f", "segment", "-reset_timestamps", "1",
         "-segment_time", str(seg_secs), str(pattern)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr[-300:] if proc.stderr else "no output"
        raise TelegramFileTooLargeError(f"Split failed for {path.name}: {detail}")
    parts = sorted(workdir.glob(f"{stem}.pt*{path.suffix}"))
    if len(parts) <= 1:
        for p in parts:
            p.unlink(missing_ok=True)
        return _split(path, limit, workdir, _depth=_depth + 1)

    result: list[Path] = []
    for part in parts:
        if part.stat().st_size <= limit:
            result.append(part)
        else:
            result.extend(_split(part, limit, workdir, _depth=_depth + 1))
    return result
