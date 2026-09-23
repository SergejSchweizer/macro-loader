from datetime import UTC, date, datetime

from application.fed_policy_macro_raw_quality import (
    compute_feature_coverage,
    diff_canonical_parity,
    semantic_quality_checks,
)


def _row(day: int, **values: float | None) -> dict[str, object]:
    return {
        "timestamp_m1": datetime(2020, 1, day, tzinfo=UTC),
        "available_at_utc": datetime(2020, 1, day, 12, tzinfo=UTC),
        **values,
    }


def test_coverage_reports_nulls_gaps_and_year_counts() -> None:
    rows = [
        _row(1, fed_next_expected_move_bp=1.0),
        _row(3, fed_next_expected_move_bp=2.0),
    ]
    coverage = compute_feature_coverage(rows, date(2020, 1, 1), date(2020, 1, 4))
    assert coverage["fed_next_expected_move_bp"].non_null_count == 2
    assert coverage["fed_next_expected_move_bp"].null_count == 2
    assert coverage["fed_next_expected_move_bp"].longest_gap_days == 1
    assert coverage["fed_next_expected_move_bp"].yearly_non_null == ((2020, 2),)


def test_parity_returns_exact_sorted_feature_mismatches() -> None:
    canonical = [_row(2, fed_next_expected_move_bp=1.0)]
    actual = [_row(2, fed_next_expected_move_bp=2.0)]
    assert diff_canonical_parity(actual, canonical) == ("2020-01-02:fed_next_expected_move_bp",)


def test_semantic_checks_fail_for_nonfinite_uncertainty_and_lookahead() -> None:
    row = _row(
        1,
        fed_next_expected_move_bp=float("inf"),
        fed_next_uncertainty_bp=float("inf"),
    )
    row["fed_next_uncertainty_bp"] = -1.0
    row["available_at_utc"] = datetime(2019, 12, 31, tzinfo=UTC)
    assert semantic_quality_checks([row]) == (
        "finite:fed_next_expected_move_bp",
        "point_in_time_availability",
        "uncertainty_non_negative",
    )
