from __future__ import annotations

import logging
import os
from datetime import date, datetime
from pathlib import Path

import pytest

from api.cli import JsonEventSink, _logger
from api.logging_config import configure_debug_logging
from scripts.rotate_operational_logs import rotate_operational_logs


def test_debug_logging_uses_logs_directory_and_ninety_daily_backups(tmp_path: Path) -> None:
    log_path = configure_debug_logging(tmp_path)
    root_logger = logging.getLogger()
    handler = next(
        handler
        for handler in root_logger.handlers
        if getattr(handler, "_macro_loader_debug_handler", False)
    )

    try:
        root_logger.getChild("tests.operational").debug("debug record")
        handler.flush()

        assert log_path == tmp_path / ".logs" / "macro-loader-debug.log"
        assert "debug record" in log_path.read_text(encoding="utf-8")
        assert handler.backupCount == 90
        assert handler.when == "MIDNIGHT"
    finally:
        root_logger.removeHandler(handler)
        handler.close()


def test_configure_debug_logging_replaces_handler_when_project_root_changes(
    tmp_path: Path,
) -> None:
    first_path = configure_debug_logging(tmp_path / "first")
    second_path = configure_debug_logging(tmp_path / "second")
    handlers = [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_macro_loader_debug_handler", False)
    ]

    assert second_path == tmp_path / "second" / ".logs" / "macro-loader-debug.log"
    assert len(handlers) == 1
    assert Path(handlers[0].baseFilename) == second_path
    assert first_path.parent.exists()
    logging.getLogger().removeHandler(handlers[0])
    handlers[0].close()


def test_structured_event_debug_record_is_sanitized_and_written_to_project_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from io import StringIO

    monkeypatch.setenv("MACRO_LOADER_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("PGPASSWORD", "sensitive-value")
    stderr = StringIO()
    sink = JsonEventSink(_logger(stderr), secrets=("sensitive-value",))
    sink({"stage": "test", "credential": "sensitive-value"})
    logging.getLogger("application.test").debug("credential=%s", "sensitive-value")
    debug_handler = next(
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_macro_loader_debug_handler", False)
    )
    debug_handler.flush()
    log_path = tmp_path / ".logs" / "macro-loader-debug.log"
    try:
        assert "sensitive-value" not in stderr.getvalue()
        assert "sensitive-value" not in log_path.read_text(encoding="utf-8")
        assert '"credential": "***"' in log_path.read_text(encoding="utf-8")
    finally:
        logging.getLogger().removeHandler(debug_handler)
        debug_handler.close()


def test_rotate_logs_archives_previous_day_prunes_only_expired_named_files(
    tmp_path: Path,
) -> None:
    today = date(2026, 10, 28)
    daily_log = tmp_path / "macro-loader.log"
    current_log = tmp_path / "macro-loader-gold-copy.log"
    old_archive = tmp_path / "macro-loader.log.2026-07-29"
    unrelated = tmp_path / "other.log.2026-01-01"
    for path in (daily_log, current_log, old_archive, unrelated):
        path.write_text(path.name, encoding="utf-8")
    os.utime(daily_log, (datetime(2026, 10, 27, 10).timestamp(),) * 2)
    os.utime(old_archive, (datetime(2026, 7, 29, 10).timestamp(),) * 2)

    rotated = rotate_operational_logs(tmp_path, today=today)

    assert rotated == (tmp_path / "macro-loader.log.2026-10-27",)
    assert not daily_log.exists()
    assert (tmp_path / "macro-loader.log.2026-10-27").read_text(encoding="utf-8") == (
        "macro-loader.log"
    )
    assert current_log.exists()
    assert not old_archive.exists()
    assert unrelated.exists()


def test_rotate_logs_refuses_to_overwrite_same_date_archive(tmp_path: Path) -> None:
    log_path = tmp_path / "macro-loader.log"
    archive = tmp_path / "macro-loader.log.2026-10-27"
    log_path.write_text("new log", encoding="utf-8")
    archive.write_text("existing archive", encoding="utf-8")
    os.utime(log_path, (datetime(2026, 10, 27, 10).timestamp(),) * 2)

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        rotate_operational_logs(tmp_path, today=date(2026, 10, 28))

    assert log_path.read_text(encoding="utf-8") == "new log"
    assert archive.read_text(encoding="utf-8") == "existing archive"


def test_rotate_logs_leaves_current_day_files_in_place(tmp_path: Path) -> None:
    current = tmp_path / "macro-loader.log"
    current.write_text("today", encoding="utf-8")
    now = datetime.combine(date.today(), datetime.min.time()).timestamp()
    os.utime(current, (now, now))

    assert rotate_operational_logs(tmp_path) == ()
    assert current.read_text(encoding="utf-8") == "today"


def test_rotate_logs_emits_counts_without_directory_paths(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="scripts.rotate_operational_logs")
    yesterday = tmp_path / "macro-loader.log"
    expired = tmp_path / "macro-loader.log.2026-07-29"
    yesterday.write_text("daily", encoding="utf-8")
    expired.write_text("expired", encoding="utf-8")
    os.utime(yesterday, (datetime(2026, 10, 27, 10).timestamp(),) * 2)

    rotate_operational_logs(tmp_path, today=date(2026, 10, 28))

    assert "cron log maintenance started" in caplog.text
    assert "rotated_count=1 pruned_archive_count=1" in caplog.text
    assert str(tmp_path) not in caplog.text


def test_rotate_logs_failure_records_only_exception_class(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="scripts.rotate_operational_logs")
    daily = tmp_path / "macro-loader.log"
    daily.write_text("daily", encoding="utf-8")
    (tmp_path / "macro-loader.log.2026-10-27").write_text("existing", encoding="utf-8")
    os.utime(daily, (datetime(2026, 10, 27, 10).timestamp(),) * 2)

    with pytest.raises(FileExistsError):
        rotate_operational_logs(tmp_path, today=date(2026, 10, 28))

    assert "cron log maintenance failed error_type=FileExistsError" in caplog.text
    assert str(tmp_path) not in caplog.text
    assert "refusing to overwrite" not in caplog.text
