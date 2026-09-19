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
