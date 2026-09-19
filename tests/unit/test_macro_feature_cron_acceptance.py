from __future__ import annotations

from scripts.macro_feature_cron_acceptance import main


def test_cron_acceptance_requires_explicit_execute(capsys) -> None:
    assert main(["--project-root", "/srv/macro-loader"]) == 2
    assert "--execute" in capsys.readouterr().out
