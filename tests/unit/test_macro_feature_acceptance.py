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
    assert commands[0][1][3:] == ("reconcile", "--series", "vix")
    assert commands[1][0] == "bootstrap:vix9d"
    assert commands[1][1][3:] == ("bootstrap", "--series", "vix9d")
    assert sum(name.startswith(("bootstrap:", "reconcile:")) for name, _ in commands) == len(
        SERIES_REGISTRY
    )
    assert commands[-2][0] == "gold-sync-postgres-replay"
    assert commands[-1][0] == "postgres-verify-replay"


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
    assert '"result": "PASS"' in report.read_text()
    assert "full-run acceptance completed result=PASS" in caplog.text
    assert str(tmp_path) not in caplog.text
    assert "/srv/lake" not in caplog.text


def test_acceptance_writes_fail_report_at_first_failed_stage(
    tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="scripts.macro_feature_acceptance")

    class Completed:
        returncode = 1

    monkeypatch.setattr(
        "scripts.macro_feature_acceptance.subprocess.run", lambda *args, **kwargs: Completed()
    )
    report = tmp_path / "full-run.json"

    assert main(["--lake-root", "/srv/lake", "--report", str(report), "--execute"]) == 1
    assert '"result": "FAIL"' in report.read_text()
    assert "full-run acceptance stage completed" in caplog.text
    assert "status=FAIL exit_code=1" in caplog.text


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
