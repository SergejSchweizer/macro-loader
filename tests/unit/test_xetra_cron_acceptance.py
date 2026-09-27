"""Acceptance checks for the installed cron wrapper and sanitized report."""

from __future__ import annotations

from pathlib import Path

from scripts.macro_feature_cron_acceptance import main


def test_installed_wrapper_uses_lock_preflight_and_canonical_commands() -> None:
    runner = Path("ops/run-macro-loader-sunday.sh").read_text()

    assert "if ! flock -n 9; then" in runner
    assert '"$CLI" --lake-root "$LAKE_ROOT" run-daily' in runner
    assert '"$CLI" --lake-root "$LAKE_ROOT" gold-sync-postgres' in runner
    assert "python -c" not in runner


def test_cron_acceptance_report_is_reproducible_with_source_date_epoch(
    tmp_path: Path, monkeypatch
) -> None:
    class Completed:
        returncode = 0

    monkeypatch.setattr(
        "scripts.macro_feature_cron_acceptance.subprocess.run", lambda *args, **kwargs: Completed()
    )
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    assert main(["--project-root", "/srv/macro-loader", "--report", str(first), "--execute"]) == 0
    assert main(["--project-root", "/srv/macro-loader", "--report", str(second), "--execute"]) == 0
    assert first.read_text() == second.read_text()
    assert "password" not in first.read_text().lower()


def test_cron_failure_propagates_nonzero_and_never_reports_pass(
    tmp_path: Path, monkeypatch
) -> None:
    class Completed:
        returncode = 7

    monkeypatch.setattr(
        "scripts.macro_feature_cron_acceptance.subprocess.run", lambda *args, **kwargs: Completed()
    )
    report = tmp_path / "failure.json"

    assert main(["--project-root", "/srv/macro-loader", "--report", str(report), "--execute"]) == 1
    assert '"result": "FAIL"' in report.read_text()
