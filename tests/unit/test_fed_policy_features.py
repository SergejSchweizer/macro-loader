from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from application.fed_policy_features import (
    FED_POLICY_FEATURE_COLUMNS,
    build_fed_policy_features,
)


def _snapshots(observations: list[date], *, second_meeting: bool = True) -> pl.DataFrame:
    rows: list[tuple[date, date, float, float, datetime]] = []
    for observation in observations:
        for meeting, outcomes in [
            (date(2026, 1, 28), ((0.0, 0.8), (25.0, 0.2))),
            *(
                [(date(2026, 3, 18), ((0.0, 0.5), (-25.0, 0.5)))]
                if second_meeting
                else []
            ),
            (date(2026, 4, 29), ((0.0, 0.25), (25.0, 0.75))),
        ]:
            for move, probability in outcomes:
                rows.append(
                    (
                        observation,
                        meeting,
                        move,
                        probability,
                        datetime(
                            observation.year,
                            observation.month,
                            observation.day,
                            23,
                            59,
                            59,
                            999999,
                            tzinfo=UTC,
                        ),
                    )
                )
    return pl.DataFrame(
        rows,
        schema={
            "observation_date": pl.Date,
            "meeting_date": pl.Date,
            "move_bp": pl.Float64,
            "probability": pl.Float64,
            "available_at_utc": pl.Datetime("us", "UTC"),
        },
        orient="row",
    )


def test_exact_fed_policy_formulas_and_three_month_selection() -> None:
    frame = build_fed_policy_features(_snapshots([date(2026, 1, 2)]))
    row = frame.row(0, named=True)
    assert tuple(frame.columns[1:]) == FED_POLICY_FEATURE_COLUMNS
    assert row["fed_next_expected_move_bp"] == pytest.approx(5.0)
    assert row["fed_next_uncertainty_bp"] == pytest.approx(10.0)
    assert row["fed_m3_expected_move_bp"] == pytest.approx(11.25)
    assert row["fed_repricing_5obs_bp"] is None


def test_only_prior_observation_is_used_for_five_observation_delta() -> None:
    observations = [date(2026, 1, 2) + timedelta(days=index) for index in range(6)]
    frame = build_fed_policy_features(_snapshots(observations))
    assert frame[-1, "fed_repricing_5obs_bp"] == pytest.approx(0.0)


def test_eod_boundary_and_invalid_probabilities_fail_closed() -> None:
    frame = _snapshots([date(2026, 1, 2)]).with_columns(
        pl.col("available_at_utc") + timedelta(microseconds=1)
    )
    with pytest.raises(ValueError, match="EOD"):
        build_fed_policy_features(frame)
    invalid = _snapshots([date(2026, 1, 2)]).with_columns(
        pl.when(pl.col("move_bp") == 25.0)
        .then(0.3)
        .otherwise(pl.col("probability"))
        .alias("probability")
    )
    with pytest.raises(ValueError, match="sum to one"):
        build_fed_policy_features(invalid)


def test_empty_snapshot_has_exact_schema() -> None:
    result = build_fed_policy_features(pl.DataFrame())
    assert result.columns == ["timestamp_m1", *FED_POLICY_FEATURE_COLUMNS]
