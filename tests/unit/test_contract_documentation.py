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


def test_delivery_and_fedwatch_documentation_describe_current_path() -> None:
    agents = (_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    provider = (_ROOT / "ingestion" / "fed_policy_provider.py").read_text(encoding="utf-8")

    assert "PR-145 through PR-150 are merged" in agents
    assert "PR-80 through PR-90" not in agents
    assert "browser-only Chinese CME FedWatch page" in provider
    assert "https://www.cmegroup.cn/fed-watch/" in provider
    assert "not a production fallback" in provider
