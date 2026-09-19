from __future__ import annotations

import math

from application.macro_feature_catalog import FEATURE_COLUMNS
from application.macro_feature_parity import compare_catalog_to_sql
from ingestion.postgres_gold_repository import _FEATURES_VIEW_DDL


def test_catalog_is_fully_projected_by_postgres_view() -> None:
    report = compare_catalog_to_sql(_FEATURES_VIEW_DDL, FEATURE_COLUMNS)
    assert report.passed
    assert report.compared_columns == FEATURE_COLUMNS


def test_dropped_or_renamed_projection_fails_closed() -> None:
    sql = _FEATURES_VIEW_DDL.replace('"usd_broad_log_return_20obs"', '"renamed_feature"')
    report = compare_catalog_to_sql(sql, FEATURE_COLUMNS)
    assert "usd_broad_log_return_20obs" in report.missing_columns


def test_pr83_hand_calculations_are_explicit() -> None:
    vix9d, vix3m = 22.0, 18.0
    assert math.isclose(math.log(vix9d / vix3m), 0.20067069546215124)
    current, prior = 105.0, 100.0
    assert math.isclose(math.log(current / prior), math.log(1.05))
