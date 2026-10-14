"""核心服务的 UTF-8 滚动日志与敏感字段遮蔽。"""
from __future__ import annotations

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(sk-[A-Za-z0-9_-]{8,})\b"),
    re.compile(r"(?i)\b(password|token|secret|api[_-]?key)\s*[=:]\s*([^\s,;]+)"),
    re.compile(r"(?i)\b(wxid_[A-Za-z0-9_-]+)\b"),
)


def redact_log_text(value: object) -> str:
    text = str(value)
    text = _SECRET_PATTERNS[0].sub("[REDACTED_KEY]", text)
    text = _SECRET_PATTERNS[1].sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _SECRET_PATTERNS[2].sub("[REDACTED_CONTACT]", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(record.msg)
        record.msg = redact_log_text(rendered)
        record.args = ()
        return True


def configure_noise_loggers() -> None:
    """保留调度异常，抑制每秒空闲轮询的成功日志。"""
    level = os.getenv("APSCHEDULER_EXECUTOR_LOG_LEVEL", "WARNING").upper()
    logging.getLogger("apscheduler.executors.default").setLevel(level)


def configure_logging() -> Path:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    project_root = Path(__file__).resolve().parents[1]
    configured = Path(os.getenv("DATA_DIR", "data"))
    data_dir = configured if configured.is_absolute() else project_root / configured
    log_dir = data_dir.resolve() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "soul-mate-core.log"
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    redactor = RedactingFilter()
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(redactor)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        handlers=[file_handler, stream_handler],
        force=True,
    )
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    configure_noise_loggers()
    return log_path
