from __future__ import annotations

from pathlib import Path

import api
import application
import ingestion
import scripts

ROOT = Path(__file__).resolve().parents[2]


def test_package_roots_are_importable() -> None:
    assert application.__doc__
    assert ingestion.__doc__
    assert api.__doc__
    assert scripts.__doc__


def test_no_github_actions_workflows_are_installed() -> None:
    workflows = ROOT / ".github/workflows"
    assert not workflows.exists() or not list(workflows.glob("*.yml"))
    assert not workflows.exists() or not list(workflows.glob("*.yaml"))


def test_makefile_enforces_coverage_and_parallel_gate() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "$(MAKE) -j4 lint type test" in makefile
    assert "test: unit integration" in makefile
    assert "coverage report" in makefile
    assert "fail_under = 85.0" in project


def test_pre_commit_hook_runs_local_quality_gate() -> None:
    text = (ROOT / ".githooks/pre-commit").read_text(encoding="utf-8")
    assert "make quality-gate" in text
    assert "git status --short" not in text


def test_repository_setup_contains_required_policy() -> None:
    text = (ROOT / "scripts/setup_github_repository.sh").read_text(encoding="utf-8")
    for check in ("lint", "type", "unit", "integration", "coverage"):
        assert f'"{check}"' in text
    assert "allow_auto_merge=true" in text
    assert "allow_squash_merge=true" in text
    assert "allow_merge_commit=false" in text
    assert "allow_rebase_merge=false" in text
    assert '"enforce_admins": true' in text
    assert '"allow_force_pushes": false' in text
    assert '"allow_deletions": false' in text


def test_auto_merge_helper_uses_squash_and_auto() -> None:
    text = (ROOT / "scripts/enable_auto_merge.sh").read_text(encoding="utf-8")
    assert 'gh pr merge "$1" --auto --squash' in text
