from __future__ import annotations

from pathlib import Path

BACKLOG = Path("BACKLOG.md")


def test_obsolete_archive_condenses_postgres_delivery_history() -> None:
    text = BACKLOG.read_text(encoding="utf-8")
    archive = text.split("## Obsolete PR Archive", maxsplit=1)[1]
    assert "PR-31–PR-61" in archive
    assert "PostgreSQL serving and operational hardening" in archive
    assert "Obsolete" in archive


def test_closed_postgres_prs_are_not_detailed_active_sections() -> None:
    text = BACKLOG.read_text(encoding="utf-8")
    for pr_number in range(31, 62):
        assert f"## PR-{pr_number:02d}:" not in text


def test_no_postgres_pr_remains_active_or_detailed() -> None:
    text = BACKLOG.read_text(encoding="utf-8")
    archive = text.split("## Obsolete PR Archive", maxsplit=1)[1]
    for pr_id in ("PR-107", "PR-108", "PR-109"):
        assert f"**{pr_id}:" in archive
    assert "## PR-107:" not in text
    assert "## PR-108:" not in text
    assert "## PR-109:" not in text
