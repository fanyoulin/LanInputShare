from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOGGER_NAME = "lan_input_share"
_SENSITIVE_PREFIXES = ("clipboard_text=", "auth_token=", "private_key=")
_WINDOWS_PATH = re.compile(r"(?i)(?:[a-z]:[\\/]|\\\\)[^\r\n\t ]+")


def redact_log_value(value: str) -> str:
    stripped = value.strip()
    lowered = stripped.lower()
    for prefix in _SENSITIVE_PREFIXES:
        if lowered.startswith(prefix):
            return f"{stripped[: len(prefix)]}[REDACTED]"
    if "-----begin private key-----" in lowered:
        return "[REDACTED]"
    return _WINDOWS_PATH.sub("[PATH]", value)


def sanitize_transfer_error(error: str | None) -> str:
    text = (error or "").casefold()
    if "取消" in text:
        return "用户取消"
    if "空间" in text or "disk" in text:
        return "磁盘空间不足"
    if "sha-256" in text or "校验" in text or "大小不匹配" in text:
        return "文件校验失败"
    if "源文件" in text or "源文件映射" in text or "清单已失效" in text:
        return "源文件已变化"
    if "断开" in text or "连接" in text or "network" in text:
        return "连接已断开"
    if (
        "目标" in text
        or "目录" in text
        or "路径" in text
        or "工作区" in text
        or "冲突" in text
    ):
        return "目标位置不可用"
    return "文件传输失败"


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = redact_log_value(message)
        record.args = ()
        return True


def configure_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = (log_dir / "lan-input-share.log").resolve()
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    for handler in tuple(logger.handlers):
        current = getattr(handler, "baseFilename", None)
        if current and Path(current).resolve() == log_file:
            return logger
        logger.removeHandler(handler)
        handler.close()

    handler = RotatingFileHandler(
        log_file,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(_RedactingFilter())
    logger.addHandler(handler)
    return logger


__all__ = ["configure_logging", "redact_log_value", "sanitize_transfer_error"]
