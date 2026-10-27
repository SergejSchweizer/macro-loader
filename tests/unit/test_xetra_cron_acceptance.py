"""Acceptance checks for the installed cron wrapper and sanitized report."""

from __future__ import annotations

from pathlib import Path

from scripts.macro_feature_cron_acceptance import main


def test_installed_wrapper_uses_lock_preflight_and_canonical_commands() -> None:
    runner = Path("ops/run-macro-loader-daily.sh").read_text()

    assert "if ! flock -n 9; then" in runner
    assert '"$CLI" --overlap-days 60 --cores "$CORES" --lake-root "$LAKE_ROOT" run-daily' in runner
    assert '"$CLI" --lake-root "$LAKE_ROOT" gold-sync-postgres' in runner
    assert "python -c" not in runner


def test_cron_acceptance_report_is_reproducible_with_source_date_epoch(
    tmp_path: Path, monkeypatch
) -> None:
    class Completed:
        def __init__(self, command):
            self.returncode = 0
            self.stdout = ""
            self.stderr = ""
            if isinstance(command, list) and len(command) > 2 and command[0] == "bash":
                shell = command[2]
                if "gold-sync-postgres" in shell:
                    self.stdout = (
                        '{"command":"gold-sync-postgres","stage":"postgres_sync",'
                        '"status":"success","inserted":0,"updated":0,"deleted":0}\n'
                    )
                else:
                    self.stdout = '{"status":"PASS","summaries":{"consumer_row_count":1}}\n'

    monkeypatch.setattr(
        "scripts.macro_feature_cron_acceptance.subprocess.run",
        lambda command, **kwargs: Completed(command),
    )
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    assert main(["--project-root", "/srv/macro-loader", "--report", str(first), "--execute"]) == 0
    assert main(["--project-root", "/srv/macro-loader", "--report", str(second), "--execute"]) == 0
    assert first.read_text() == second.read_text()
    assert "password" not in first.read_text().lower()
    assert '"name": "zero-change-replay"' in first.read_text()
    assert '"name": "independent-postgres-verify"' in first.read_text()


def test_cron_failure_propagates_nonzero_and_never_reports_pass(
    tmp_path: Path, monkeypatch
) -> None:
    class Completed:
        returncode = 7
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        "scripts.macro_feature_cron_acceptance.subprocess.run", lambda *args, **kwargs: Completed()
    )
    report = tmp_path / "failure.json"

    assert main(["--project-root", "/srv/macro-loader", "--report", str(report), "--execute"]) == 1
    assert '"result": "FAIL"' in report.read_text()
