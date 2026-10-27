from __future__ import annotations

from datetime import date

import polars as pl

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


def test_acceptance_requires_explicit_execute(capsys) -> None:
    assert main(["--lake-root", "/srv/lake"]) == 2
    assert "--execute" in capsys.readouterr().out


def test_acceptance_writes_pass_report_after_all_stages(tmp_path, monkeypatch) -> None:
    class Completed:
        returncode = 0

    monkeypatch.setattr(
        "scripts.macro_feature_acceptance.subprocess.run", lambda *args, **kwargs: Completed()
    )
    report = tmp_path / "full-run.json"

    assert main(["--lake-root", "/srv/lake", "--report", str(report), "--execute"]) == 0
    assert '"result": "PASS"' in report.read_text()


def test_acceptance_writes_fail_report_at_first_failed_stage(tmp_path, monkeypatch) -> None:
    class Completed:
        returncode = 1

    monkeypatch.setattr(
        "scripts.macro_feature_acceptance.subprocess.run", lambda *args, **kwargs: Completed()
    )
    report = tmp_path / "full-run.json"

    assert main(["--lake-root", "/srv/lake", "--report", str(report), "--execute"]) == 1
    assert '"result": "FAIL"' in report.read_text()
