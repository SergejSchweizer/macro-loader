from __future__ import annotations

import json
from pathlib import Path

from scripts.fed_policy_cron_acceptance import main


def test_cron_acceptance_requires_explicit_execute(capsys) -> None:
    assert main(["--project-root", "/srv/macro-loader"]) == 2
    assert "--execute" in capsys.readouterr().out


def test_cron_acceptance_invokes_installed_wrapper_twice(tmp_path: Path, monkeypatch) -> None:
    class Completed:
        returncode = 0

    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        "scripts.fed_policy_cron_acceptance.subprocess.run",
        lambda command, **kwargs: (calls.append((*command, kwargs["cwd"])), Completed())[1],
    )
    report = tmp_path / "cron.json"

    assert main(["--project-root", "/srv/macro-loader", "--report", str(report), "--execute"]) == 0
    payload = json.loads(report.read_text())
    assert payload["result"] == "PASS"
    assert payload["wrapper"] == "ops/run-macro-loader-sunday.sh"
    assert len(calls) == 2
    assert all(call[0] == "/srv/macro-loader/ops/run-macro-loader-sunday.sh" for call in calls)


def test_cron_acceptance_stops_after_failed_run(tmp_path: Path, monkeypatch) -> None:
    class Completed:
        returncode = 1

    calls = 0

    def run(*args: object, **kwargs: object) -> Completed:
        nonlocal calls
        calls += 1
        return Completed()

    monkeypatch.setattr("scripts.fed_policy_cron_acceptance.subprocess.run", run)
    report = tmp_path / "cron.json"

    assert main(["--project-root", "/srv/macro-loader", "--report", str(report), "--execute"]) == 1
    assert json.loads(report.read_text())["result"] == "FAIL"
    assert calls == 1
