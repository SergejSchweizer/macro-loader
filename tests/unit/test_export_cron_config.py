from __future__ import annotations

import logging
import shlex
from pathlib import Path

import pytest

from scripts.export_cron_config import export


def _config(
    *,
    reader_password: str = "reader secret",
    writer_password: str = "writer secret",
    user: str = "macro_reader",
) -> str:
    return (
        "runtime:\n"
        "  home: /home/a\n"
        "  path: /bin\n"
        "  project_root: /project path\n"
        "  lake_root: /lake\n"
        "  log_path: /project path/.logs/macro-loader.log\n"
        "  ssl_cert_file: /cert\n"
        "  gold_mirror_root: /mirror\n"
        "  postgres_host: 10.10.1.3\n"
        '  postgres_port: "54321"\n'
        f"  postgres_user: {user}\n"
        "  postgres_database: macro_loader\n"
        "secrets:\n"
        "  fred_api_key: secret key\n"
        f"  postgres_reader_password: {reader_password!r}\n"
        f"  postgres_writer_password: {writer_password!r}\n"
        "postgres_admin_password: admin secret\n"
    )


def _exports(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        prefix, assignment = line.split(" ", 1)
        assert prefix == "export"
        name, value = assignment.split("=", 1)
        parsed = shlex.split(f"x={value}")[0]
        result[name] = parsed.split("=", 1)[1]
    return result


def test_export_cron_config_quotes_required_runtime_and_postgres_values(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(_config(), encoding="utf-8")
    values = _exports(export(config))
    assert values["FRED_API_KEY"] == "secret key"
    assert values["PROJECT_ROOT"] == "/project path"
    assert values["PGHOST"] == "10.10.1.3"
    assert values["PGPORT"] == "54321"
    assert values["PGUSER"] == "macro_reader"
    assert values["PGDATABASE"] == "macro_loader"
    assert values["PGPASSWORD"] == "reader secret"
    assert values["PGSYNCUSER"] == "macro_writer"
    assert values["PGSYNCPASSWORD"] == "writer secret"
    assert values["PGREADUSER"] == "macro_reader"
    assert values["PGREADPASSWORD"] == "reader secret"
    assert all("ADMIN" not in name for name in values)
    assert "admin secret" not in export(config)
    assert values["LOG_PATH"] == "/project path/.logs/macro-loader.log"


def test_export_rejects_wrong_postgres_endpoint_or_user(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(_config().replace("10.10.1.3", "localhost"), encoding="utf-8")
    with pytest.raises(ValueError, match="postgres_host"):
        export(config)

    config.write_text(_config(user="postgres"), encoding="utf-8")
    with pytest.raises(ValueError, match="postgres_user"):
        export(config)


def test_export_rejects_missing_postgres_password_without_leaking_secret(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        _config().replace("  postgres_reader_password: 'reader secret'\n", ""),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="postgres_reader_password") as exc_info:
        export(config)
    assert "reader secret" not in str(exc_info.value)


def test_export_rejects_noncanonical_log_path(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        _config().replace(
            "/project path/.logs/macro-loader.log",
            "/var/log/macro-loader.log",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"\.logs/macro-loader\.log"):
        export(config)


def test_export_rejects_config_for_a_different_installed_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(_config(), encoding="utf-8")
    monkeypatch.setenv("MACRO_LOADER_PROJECT_ROOT", "/installed/project")
    with pytest.raises(ValueError, match="installed project root"):
        export(config)


def test_export_logs_variable_count_without_paths_or_values(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="scripts.export_cron_config")
    config = tmp_path / "config.yaml"
    config.write_text(_config(reader_password="very-secret"), encoding="utf-8")

    result = export(config)

    assert "cron configuration export started" in caplog.text
    assert f"variable_count={len(result.splitlines())}" in caplog.text
    assert str(tmp_path) not in caplog.text
    assert "very-secret" not in caplog.text
    assert "writer secret" not in caplog.text


def test_export_failure_logs_only_exception_class_and_no_config_values(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="scripts.export_cron_config")
    config = tmp_path / "config.yaml"
    config.write_text(_config().replace("10.10.1.3", "invalid-host"), encoding="utf-8")

    with pytest.raises(ValueError, match="postgres_host"):
        export(config)

    assert "cron configuration export failed error_type=ValueError" in caplog.text
    assert "invalid-host" not in caplog.text
    assert "must be 10.10.1.3" not in caplog.text
    assert str(tmp_path) not in caplog.text


def test_repository_ignores_protected_config_and_logs() -> None:
    gitignore = Path(".gitignore").read_text(encoding="utf-8")
    assert "config.yaml" in gitignore
    assert ".logs/" in gitignore
