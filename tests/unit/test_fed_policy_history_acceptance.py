from __future__ import annotations

import json
from pathlib import Path

from scripts.fed_policy_history_acceptance import _commands, main


def test_acceptance_plan_has_history_and_bounded_replay() -> None:
    commands = _commands(Path("/srv/lake"), "2026-09-23")
    assert [name for name, _ in commands] == [
        "fed-policy-reconcile-2010",
        "fed-policy-postgres-sync",
        "fed-policy-postgres-verify",
        "fed-policy-bounded-replay",
        "fed-policy-replay-postgres-sync",
        "fed-policy-replay-postgres-verify",
    ]
    assert all("--today" in command for _, command in commands)
    assert commands[0][1][-1] == "fed-policy-reconcile"


def test_acceptance_requires_explicit_execute(tmp_path: Path, capsys) -> None:
    report = tmp_path / "history.json"
    assert main(["--lake-root", "/srv/lake", "--report", str(report)]) == 2
    assert "--execute" in capsys.readouterr().out
    assert json.loads(report.read_text())["result"] == "FAIL"


def test_acceptance_stops_at_first_failure_and_never_passes(tmp_path: Path, monkeypatch) -> None:
    class Completed:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_: object) -> Completed:
        calls.append(command)
        return Completed(1 if len(calls) == 2 else 0)

    monkeypatch.setattr("scripts.fed_policy_history_acceptance.subprocess.run", run)
    report = tmp_path / "history.json"
    assert main(["--lake-root", "/srv/lake", "--report", str(report), "--execute"]) == 1
    payload = json.loads(report.read_text())
    assert payload["result"] == "FAIL"
    assert [stage["exit_code"] for stage in payload["stages"]] == [0, 1]


def test_acceptance_passes_only_after_every_stage(tmp_path: Path, monkeypatch) -> None:
    class Completed:
        returncode = 0

    monkeypatch.setattr(
        "scripts.fed_policy_history_acceptance.subprocess.run", lambda *args, **kwargs: Completed()
    )
    report = tmp_path / "history.json"
    assert main(["--lake-root", "/srv/lake", "--report", str(report), "--execute"]) == 0
    payload = json.loads(report.read_text())
    assert payload["result"] == "PASS"
    assert len(payload["stages"]) == 6
