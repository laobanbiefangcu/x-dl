"""loguru 配置：stdout + 滚动文件，统一供 bot 和 sync 使用。

也把 stdlib logging 转发到 loguru，让 gallery-dl / aiogram 等三方库日志走同一管道。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from loguru import logger as _logger

_CONFIGURED = False


class _InterceptHandler(logging.Handler):
    """把 stdlib logging 的记录转发到 loguru。"""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            level = _logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        depth = 2
        frame = logging.currentframe()
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        _logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup(log_file: Path | None = None, level: str = "INFO") -> None:
    """幂等。多次调用只生效第一次。"""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    _logger.remove()
    _logger.add(
        sys.stdout,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | "
               "<cyan>{name}</cyan> - <level>{message}</level>",
        enqueue=False,
        backtrace=False,
        diagnose=False,
    )
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        _logger.add(
            str(log_file),
            level=level,
            rotation="10 MB",
            retention=10,
            compression="gz",
            encoding="utf-8",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level:<7} | {name}:{function}:{line} - {message}",
            enqueue=True,
        )

    # stdlib → loguru
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for name in ("aiogram", "aiogram.event", "aiogram.dispatcher",
                 "aiohttp.access", "gallery_dl", "urllib3"):
        lg = logging.getLogger(name)
        lg.handlers = [_InterceptHandler()]
        lg.propagate = False


def get(name: str = "xdl"):
    return _logger.bind(name=name)
