from __future__ import annotations

from pathlib import Path

from scripts.macro_feature_acceptance import _commands, main


def test_acceptance_plan_covers_every_registered_series_and_replay() -> None:
    commands = _commands(Path("/srv/lake"))
    assert commands[0][0].startswith("reconcile:")
    assert sum(name.startswith("reconcile:") for name, _ in commands) == 13
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
