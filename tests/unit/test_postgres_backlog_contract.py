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
    for pr_id in ("PR-82", "PR-84", "PR-85", "PR-88", "PR-89", "PR-90"):
        assert f"## {pr_id}:" in text
    assert "real PostgreSQL" in text
    assert "full historical pipeline acceptance run" in text.lower()
    assert "ops/run-macro-loader-sunday.sh" in text
