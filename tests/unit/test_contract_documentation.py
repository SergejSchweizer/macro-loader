"""Executable checks for the operator and agent documentation contract."""

from pathlib import Path

from application.gold_frame import GOLD_FEATURE_VERSION, GOLD_SCHEMA_VERSION

_ROOT = Path(__file__).parents[2]


def test_current_gold_versions_are_documented_without_legacy_pair() -> None:
    for name in ("README.md", "ARCHITECTURE.md"):
        content = " ".join((_ROOT / name).read_text(encoding="utf-8").split())
        assert f"schema version **{GOLD_SCHEMA_VERSION}**" in content
        assert f"feature version **{GOLD_FEATURE_VERSION}**" in content
        assert "schema version **6** and feature version **5**" not in content
