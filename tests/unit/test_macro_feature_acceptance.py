from __future__ import annotations

import logging
from datetime import date

import polars as pl
import pytest

from application.paths import LakePaths
from application.registry import SERIES_REGISTRY
from scripts.macro_feature_acceptance import _commands, main


def test_acceptance_bootstraps_empty_series_and_reconciles_existing_series(tmp_path) -> None:
    lake_root = tmp_path / "lake"
    existing_bronze = LakePaths(lake_root).bronze_month(
        SERIES_REGISTRY["vix"].provider, "vix", date(2026, 10, 1)
    )
    existing_bronze.parent.mkdir(parents=True)
    pl.DataFrame({"observation_date": [date(2026, 10, 1)]}).write_parquet(existing_bronze)

    commands = _commands(lake_root)

    assert commands[0][0] == "reconcile:vix"
    assert commands[0][1][3:6] == ("--cores", "6", "reconcile")
    assert commands[0][1][6:] == ("--series", "vix")
    assert commands[1][0].startswith("bootstrap:vix9d,")
    assert commands[1][1][3:6] == ("--cores", "6", "bootstrap")
    assert all(argument == "--series" for argument in commands[1][1][6::2])
    assert len(commands[1][1][7::2]) == len(SERIES_REGISTRY) - 1
    assert commands[-2][0] == "gold-sync-postgres-replay"
    assert commands[-1][0] == "postgres-verify-replay"


def test_acceptance_forwards_custom_core_limit_and_rejects_non_positive_values(tmp_path) -> None:
    commands = _commands(tmp_path / "lake", cores=3)
    source_commands = [
        command for name, command in commands if name.startswith(("bootstrap:", "reconcile:"))
    ]
    assert len(source_commands) == 1
    assert source_commands[0][3:5] == ("--cores", "3")
    with pytest.raises(ValueError, match="positive integer"):
        _commands(tmp_path / "lake", cores=0)


def test_acceptance_requires_explicit_execute(
    capsys, caplog: pytest.LogCaptureFixture, monkeypatch
) -> None:
    caplog.set_level(logging.DEBUG, logger="scripts.macro_feature_acceptance")
    monkeypatch.setattr(
        "scripts.macro_feature_acceptance.subprocess.run",
        lambda *args, **kwargs: pytest.fail("dry-run must not start subprocesses"),
    )
    assert main(["--lake-root", "/srv/lake"]) == 2
    assert "--execute" in capsys.readouterr().out
    assert "full-run acceptance plan completed command_count=" in caplog.text
    assert "execute=False" in caplog.text
    assert "/srv/lake" not in caplog.text


def test_acceptance_rejects_invalid_core_count_before_planning_or_execution(
    capsys, monkeypatch
) -> None:
    monkeypatch.setattr(
        "scripts.macro_feature_acceptance._commands",
        lambda *args, **kwargs: pytest.fail("invalid core count must fail before planning"),
    )
    monkeypatch.setattr(
        "scripts.macro_feature_acceptance.subprocess.run",
        lambda *args, **kwargs: pytest.fail("invalid core count must not execute"),
    )
    with pytest.raises(SystemExit) as error:
        main(["--lake-root", "/srv/lake", "--cores", "0", "--execute"])
    assert error.value.code == 2
    assert "positive integer" in capsys.readouterr().err


def test_acceptance_writes_pass_report_after_all_stages(
    tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="scripts.macro_feature_acceptance")

    class Completed:
        returncode = 0

    monkeypatch.setattr(
        "scripts.macro_feature_acceptance.subprocess.run", lambda *args, **kwargs: Completed()
    )
    report = tmp_path / "full-run.json"

    assert main(["--lake-root", "/srv/lake", "--report", str(report), "--execute"]) == 0
    report_payload = report.read_text()
    assert '"schema": "macro-feature-full-run-v2"' in report_payload
    assert '"result": "PASS"' in report_payload
    assert '"name": "bootstrap:vix,vix9d,' in report_payload
    assert '"name": "silver-build"' in report_payload
    assert "full-run acceptance completed result=PASS" in caplog.text
    assert str(tmp_path) not in caplog.text
    assert "/srv/lake" not in caplog.text


def test_acceptance_writes_fail_report_at_first_failed_stage(
    tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = 0

    caplog.set_level(logging.DEBUG, logger="scripts.macro_feature_acceptance")

    class Completed:
        returncode = 1

    def fail_first_batch(*args, **kwargs):
        nonlocal calls
        calls += 1
        return Completed()

    monkeypatch.setattr("scripts.macro_feature_acceptance.subprocess.run", fail_first_batch)
    report = tmp_path / "full-run.json"

    assert main(["--lake-root", "/srv/lake", "--report", str(report), "--execute"]) == 1
    assert '"result": "FAIL"' in report.read_text()
    assert "full-run acceptance stage completed" in caplog.text
    assert "status=FAIL exit_code=1" in caplog.text
    assert calls == 1


def test_acceptance_stage_exception_logs_type_only(tmp_path, monkeypatch, caplog) -> None:
    def fail(*args, **kwargs):
        raise RuntimeError("credential and command output")

    monkeypatch.setattr("scripts.macro_feature_acceptance.subprocess.run", fail)
    caplog.set_level(logging.DEBUG, logger="scripts.macro_feature_acceptance")

    with pytest.raises(RuntimeError, match="credential and command output"):
        main(["--lake-root", "/srv/lake", "--report", str(tmp_path / "failure.json"), "--execute"])

    assert "error_type=RuntimeError" in caplog.text
    assert "credential and command output" not in caplog.text
    assert str(tmp_path) not in caplog.text
