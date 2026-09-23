from __future__ import annotations

from pathlib import Path

BACKLOG = Path("BACKLOG.md")


def test_closed_delivery_summary_preserves_postgres_history() -> None:
    text = BACKLOG.read_text(encoding="utf-8")
    summary = text.split("## Closed Delivery Summary", maxsplit=1)[1]
    assert "PR-31–39" in summary
    assert "PR-40–61" in summary
    assert "PostgreSQL serving" in summary
    assert "authoritative production reconstruction" in summary


def test_closed_postgres_prs_are_not_detailed_active_sections() -> None:
    text = BACKLOG.read_text(encoding="utf-8")
    for pr_number in range(31, 62):
        assert f"## PR-{pr_number:02d}:" not in text


def test_active_postgres_feature_program_has_real_qa_and_operational_qa() -> None:
    text = BACKLOG.read_text(encoding="utf-8")
    summary = text.split("## Closed Delivery Summary", maxsplit=1)[1]
    for range_label in ("PR-80–81", "PR-82–85", "PR-86–88", "PR-89–90"):
        assert range_label in summary
    for pr_id in ("PR-107", "PR-108", "PR-109"):
        assert f"## {pr_id}:" in text
    assert "real PostgreSQL" in text
    assert "full fed policy history acceptance" in text.lower()
    assert "installed daily cron acceptance" in text.lower()
