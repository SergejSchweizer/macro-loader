from __future__ import annotations

from scripts.macro_feature_cron_acceptance import main


def test_cron_acceptance_requires_explicit_execute(capsys) -> None:
    assert main(["--project-root", "/srv/macro-loader"]) == 2
    assert "--execute" in capsys.readouterr().out


def test_cron_acceptance_writes_pass_report_after_replay(tmp_path, monkeypatch) -> None:
    class Completed:
        returncode = 0

    monkeypatch.setattr(
        "scripts.macro_feature_cron_acceptance.subprocess.run", lambda *args, **kwargs: Completed()
    )
    report = tmp_path / "cron.json"

    assert main(["--project-root", "/srv/macro-loader", "--report", str(report), "--execute"]) == 0
    assert '"result": "PASS"' in report.read_text()


def test_cron_acceptance_stops_after_failed_mutation_run(tmp_path, monkeypatch) -> None:
    class Completed:
        returncode = 1

    monkeypatch.setattr(
        "scripts.macro_feature_cron_acceptance.subprocess.run", lambda *args, **kwargs: Completed()
    )
    report = tmp_path / "cron.json"

    assert main(["--project-root", "/srv/macro-loader", "--report", str(report), "--execute"]) == 1
    assert '"result": "FAIL"' in report.read_text()
