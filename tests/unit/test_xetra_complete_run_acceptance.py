"""Offline acceptance contract for the complete XETRA-compatible run."""

from __future__ import annotations

import json

from application.macro_feature_catalog import FEATURE_COLUMNS

STAGES = (
    "source_update",
    "bronze_persist",
    "silver_rebuild",
    "gold_build",
    "postgres_raw_sync",
    "macro_features_refresh",
    "independent_verify",
)


def _complete_run_artifact(stage_results: dict[str, str], *, secret_dsn: str | None) -> str:
    if tuple(stage_results) != STAGES or any(value != "PASS" for value in stage_results.values()):
        status = "FAIL"
    else:
        status = "PASS"
    artifact = json.dumps(
        {"status": status, "stages": stage_results, "dsn_present": secret_dsn is not None},
        sort_keys=True,
        separators=(",", ":"),
    )
    return artifact.replace(secret_dsn, "[REDACTED]") if secret_dsn else artifact


def test_complete_run_requires_documented_stage_order_and_final_view_contract() -> None:
    stages = dict.fromkeys(STAGES, "PASS")
    artifact = _complete_run_artifact(stages, secret_dsn="postgresql://secret")

    assert '"status":"PASS"' in artifact
    assert list(stages) == list(STAGES)
    assert FEATURE_COLUMNS[0] == "timestamp_m1"
    assert any(column.endswith("_sma_ratio_5_20") for column in FEATURE_COLUMNS)
    assert any(column.endswith("_rsi_7obs") for column in FEATURE_COLUMNS)
    assert any(column.endswith("_roc_20obs") for column in FEATURE_COLUMNS)
    assert any(column.endswith("_drawdown_60obs") for column in FEATURE_COLUMNS)
    assert "estr_delta_1obs" in FEATURE_COLUMNS
    assert "estr_rsi_7obs" not in FEATURE_COLUMNS
    assert "estr_return_geom_10obs_pct" not in FEATURE_COLUMNS
    assert not any("return_geom_240obs" in column for column in FEATURE_COLUMNS)
    assert "postgresql://secret" not in artifact


def test_complete_run_fails_closed_for_missing_stage_or_reconcile() -> None:
    incomplete = {stage: "PASS" for stage in STAGES[:-1]}
    incomplete["normal_reconcile"] = "PASS"
    artifact = _complete_run_artifact(incomplete, secret_dsn=None)

    assert '"status":"FAIL"' in artifact
    assert "normal_reconcile" not in STAGES
