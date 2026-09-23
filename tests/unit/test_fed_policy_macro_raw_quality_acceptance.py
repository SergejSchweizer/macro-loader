import json
from pathlib import Path

from scripts.fed_policy_macro_raw_quality_acceptance import _commands, main


def test_acceptance_plan_is_explicit_and_bounded() -> None:
    commands = _commands(Path("/srv/lake"), "postgres://db", "2026-09-23")
    assert [name for name, _ in commands] == [
        "fed-policy-macro-raw-reconcile",
        "fed-policy-macro-raw-sync",
        "fed-policy-macro-raw-verify",
    ]
    assert all("--execute" not in command for _, command in commands)
    assert all("--dsn" in command for _, command in commands)


def test_acceptance_requires_explicit_execute(tmp_path: Path, capsys) -> None:
    report = tmp_path / "quality.json"
    assert main(["--lake-root", "/srv/lake", "--report", str(report)]) == 2
    assert "--execute" in capsys.readouterr().out
    assert json.loads(report.read_text())["result"] == "FAIL"


def test_acceptance_stops_on_failure(tmp_path: Path, monkeypatch) -> None:
    class Completed:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_: object) -> Completed:
        calls.append(command)
        return Completed(1 if len(calls) == 2 else 0)

    monkeypatch.setattr("scripts.fed_policy_macro_raw_quality_acceptance.subprocess.run", run)
    report = tmp_path / "quality.json"
    assert main(["--lake-root", "/srv/lake", "--report", str(report), "--execute"]) == 1
    assert [stage["exit_code"] for stage in json.loads(report.read_text())["stages"]] == [0, 1]
