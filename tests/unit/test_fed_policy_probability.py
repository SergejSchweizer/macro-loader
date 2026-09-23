from datetime import UTC, date, datetime

import polars as pl
import pytest

from application.fed_policy_probability import (
    METHODOLOGY_FINGERPRINT,
    METHODOLOGY_VERSION,
    reconstruct_meeting_distribution,
    reconstruct_probability_tree,
)


def test_no_change_curve_is_one_point_mass_on_zero() -> None:
    outcomes = reconstruct_meeting_distribution(
        {"JAN 26": 96.36, "FEB 26": 96.36}, date(2026, 1, 27), 3.64
    )

    assert outcomes[0].move_bp == pytest.approx(0.0)
    assert outcomes[0].probability == pytest.approx(1.0)
    assert sum(outcome.probability for outcome in outcomes) == pytest.approx(1.0)


def test_one_step_and_late_month_weighting_are_quarter_point_distributions() -> None:
    outcomes = reconstruct_meeting_distribution(
        {"JAN 26": 96.36, "FEB 26": 96.11}, date(2026, 1, 2), 3.64
    )

    assert outcomes[0].move_bp == pytest.approx(25.0)
    assert outcomes[0].probability == pytest.approx(1.0)


def test_tree_is_sorted_versioned_and_skips_past_meetings() -> None:
    result = reconstruct_probability_tree(
        {"JAN 26": 96.36, "FEB 26": 96.11, "MAR 26": 95.86},
        date(2026, 1, 2),
        (date(2026, 1, 28), date(2025, 12, 17), date(2026, 3, 18)),
        3.64,
        datetime(2026, 1, 2, 23, 59, 59, 999999, tzinfo=UTC),
    )

    assert result.schema["observation_date"] == pl.Date
    assert result.get_column("meeting_date").unique().sort().to_list() == [
        date(2026, 1, 28),
        date(2026, 3, 18),
    ]
    assert result.get_column("methodology_version").unique().to_list() == [METHODOLOGY_VERSION]
    assert result.group_by("meeting_date").agg(pl.col("probability").sum()).get_column(
        "probability"
    ).to_list() == [1.0, 1.0]
    assert len(METHODOLOGY_FINGERPRINT) == 64


def test_missing_or_invalid_curve_fails_closed() -> None:
    assert reconstruct_meeting_distribution({}, date(2026, 1, 28), 3.64) == ()
    with pytest.raises(ValueError, match="finite positive"):
        reconstruct_meeting_distribution(
            {"JAN 26": 96.36, "FEB 26": float("nan")}, date(2026, 1, 28), 3.64
        )
