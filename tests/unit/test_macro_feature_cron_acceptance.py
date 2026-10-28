from __future__ import annotations

import logging

import pytest

from scripts.macro_feature_cron_acceptance import main


def test_cron_acceptance_requires_explicit_execute(
    capsys, caplog: pytest.LogCaptureFixture, monkeypatch
) -> None:
    caplog.set_level(logging.DEBUG, logger="scripts.macro_feature_cron_acceptance")
    monkeypatch.setattr(
        "scripts.macro_feature_cron_acceptance.subprocess.run",
        lambda *args, **kwargs: pytest.fail("dry-run must not start subprocesses"),
    )
    assert main(["--project-root", "/srv/macro-loader"]) == 2
    assert "--execute" in capsys.readouterr().out
    assert "daily-cron acceptance started execute=False" in caplog.text
    assert "execute_flag_required" in caplog.text
    assert "/srv/macro-loader" not in caplog.text


def test_cron_acceptance_writes_pass_report_after_replay(
    tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="scripts.macro_feature_cron_acceptance")

    class Completed:
        def __init__(self, command):
            self.returncode = 0
            self.stdout = ""
            self.stderr = ""
            if isinstance(command, list) and command[0] == "bash":
                if "gold-sync-postgres" in command[2]:
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
    report = tmp_path / "cron.json"

    assert main(["--project-root", "/srv/macro-loader", "--report", str(report), "--execute"]) == 0
    assert '"result": "PASS"' in report.read_text()
    assert '"name": "zero-change-replay"' in report.read_text()
    assert '"name": "independent-postgres-verify"' in report.read_text()
    assert "daily-cron acceptance replay completed status=PASS" in caplog.text
    assert "daily-cron acceptance verification completed status=PASS" in caplog.text
    assert "daily-cron acceptance completed result=PASS stage_count=4" in caplog.text
    assert str(tmp_path) not in caplog.text


def test_cron_acceptance_rejects_a_successful_replay_that_mutates_rows(
    tmp_path, monkeypatch
) -> None:
    class Completed:
        returncode = 0
        stderr = ""

        def __init__(self, command):
            self.stdout = ""
            if isinstance(command, list) and command[0] == "bash":
                self.stdout = (
                    '{"command":"gold-sync-postgres","stage":"postgres_sync",'
                    '"status":"success","inserted":0,"updated":1,"deleted":0}\n'
                    if "gold-sync-postgres" in command[2]
                    else '{"status":"PASS"}\n'
                )

    monkeypatch.setattr(
        "scripts.macro_feature_cron_acceptance.subprocess.run",
        lambda command, **kwargs: Completed(command),
    )
    report = tmp_path / "cron.json"

    assert main(["--project-root", "/srv/macro-loader", "--report", str(report), "--execute"]) == 1
    assert '"result": "FAIL"' in report.read_text()


def test_cron_acceptance_stops_after_failed_mutation_run(tmp_path, monkeypatch) -> None:
    class Completed:
        returncode = 1
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        "scripts.macro_feature_cron_acceptance.subprocess.run", lambda *args, **kwargs: Completed()
    )
    report = tmp_path / "cron.json"

    assert main(["--project-root", "/srv/macro-loader", "--report", str(report), "--execute"]) == 1
    assert '"result": "FAIL"' in report.read_text()


def test_cron_acceptance_subprocess_exception_logs_type_only(tmp_path, monkeypatch, caplog) -> None:
    def fail(*args, **kwargs):
        raise RuntimeError("credential and subprocess output")

    monkeypatch.setattr("scripts.macro_feature_cron_acceptance.subprocess.run", fail)
    caplog.set_level(logging.DEBUG, logger="scripts.macro_feature_cron_acceptance")

    with pytest.raises(RuntimeError, match="credential and subprocess output"):
        main(
            [
                "--project-root",
                str(tmp_path),
                "--report",
                str(tmp_path / "failure.json"),
                "--execute",
            ]
        )

    assert "error_type=RuntimeError" in caplog.text
    assert "credential and subprocess output" not in caplog.text
    assert str(tmp_path) not in caplog.text
