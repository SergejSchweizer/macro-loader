"""Central debug-file logging configuration for application and adapter modules."""

from __future__ import annotations

import logging
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_HANDLER_MARKER = "_macro_loader_debug_handler"
_DEBUG_LOG_NAME = "macro-loader-debug.log"
_RETENTION_DAYS = 90


class SecretRedactionFilter(logging.Filter):
    """Remove configured secret strings from every record before file output."""

    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__()
        self._secrets = tuple(secret for secret in secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return False
        for secret in self._secrets:
            message = message.replace(secret, "***")
        record.msg = message
        record.args = ()
        return True


def configure_debug_logging(
    project_root: Path,
    *,
    secrets: tuple[str, ...] = (),
) -> Path:
    """Route Python module debug records to `.logs` with 90 daily backups."""
    log_directory = project_root / ".logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = (log_directory / _DEBUG_LOG_NAME).resolve()

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    for existing in tuple(root_logger.handlers):
        if not getattr(existing, _HANDLER_MARKER, False):
            continue
        if Path(getattr(existing, "baseFilename", "")) == log_path:
            for log_filter in tuple(existing.filters):
                if isinstance(log_filter, SecretRedactionFilter):
                    existing.removeFilter(log_filter)
            existing.addFilter(SecretRedactionFilter(secrets))
            return log_path
        root_logger.removeHandler(existing)
        existing.close()

    handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        interval=1,
        backupCount=_RETENTION_DAYS,
        encoding="utf-8",
        utc=True,
    )
    setattr(handler, _HANDLER_MARKER, True)
    handler.setLevel(logging.DEBUG)
    handler.addFilter(SecretRedactionFilter(secrets))
    formatter = logging.Formatter(
        fmt="%(asctime)sZ %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
    return log_path
