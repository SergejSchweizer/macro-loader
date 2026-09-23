from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import polars as pl
import pytest

from application.fed_policy_features import build_fed_policy_features
from application.fed_policy_probability import reconstruct_meeting_distribution

TOLERANCE = 1e-9
MEETINGS = (date(2026, 9, 16), date(2026, 10, 28), date(2026, 12, 9))


@pytest.fixture
def sanitized_cme_fixture() -> dict[str, object]:
    return {
        "fixture_id": "sanitized-cme-meeting-export-2026-08-01",
        "settlements": {"SEP 26": 96.125, "OCT 26": 96.25, "DEC 26": 96.375},
        "baseline_percent": 3.75,
        "official": {
            "2026-09-16": [(25.0, 1.0), (50.0, 0.0)],
            "2026-10-28": [(-100.0, 0.875), (-75.0, 0.125)],
            "2026-12-09": [(-25.0, 0.6739130434782616), (0.0, 0.32608695652173836)],
        },
    }


def test_reconstruction_matches_sanitized_official_probability_buckets(sanitized_cme_fixture):
    for meeting_text, expected in sanitized_cme_fixture["official"].items():
        actual = reconstruct_meeting_distribution(
            sanitized_cme_fixture["settlements"],
            date.fromisoformat(meeting_text),
            sanitized_cme_fixture["baseline_percent"],
        )
        assert [(item.move_bp, item.probability) for item in actual] == pytest.approx(
            expected, abs=TOLERANCE
        )
        assert sum(item.probability for item in actual) == pytest.approx(1.0, abs=TOLERANCE)


def test_all_four_features_match_independent_fixture_calculation():
    available = datetime(2026, 8, 1, 23, 59, 59, 999999, tzinfo=UTC)
    rows = []
    expected = []
    for offset in range(6):
        observation = date(2026, 8, 1) + timedelta(days=offset)
        rows.extend(
            {
                "observation_date": observation,
                "meeting_date": meeting,
                "move_bp": move,
                "probability": probability,
                "available_at_utc": available,
            }
            for meeting in MEETINGS
            for move, probability in ((25.0, 0.5), (50.0, 0.5))
        )
        expected.append((37.5, 12.5, 0.0, None if offset < 5 else 0.0))
    frame = pl.DataFrame(
        rows,
        schema={
            "observation_date": pl.Date,
            "meeting_date": pl.Date,
            "move_bp": pl.Float64,
            "probability": pl.Float64,
            "available_at_utc": pl.Datetime("us", "UTC"),
        },
    )
    result = build_fed_policy_features(frame)
    assert result.select(
        [
            "fed_next_expected_move_bp",
            "fed_next_uncertainty_bp",
            "fed_path_slope_m3_bp",
            "fed_repricing_5obs_bp",
        ]
    ).rows() == pytest.approx(expected, abs=TOLERANCE, nan_ok=False)


def test_formula_mutation_is_detected_by_golden_distribution(sanitized_cme_fixture):
    actual = reconstruct_meeting_distribution(
        sanitized_cme_fixture["settlements"],
        MEETINGS[0],
        sanitized_cme_fixture["baseline_percent"],
    )
    altered = tuple((item.move_bp, item.probability + 0.01) for item in actual)
    expected = sanitized_cme_fixture["official"]["2026-09-16"]
    assert altered != pytest.approx(expected, abs=TOLERANCE)


def test_availability_boundaries_are_causal():
    available = datetime(2026, 8, 1, 23, 59, 59, 999999, tzinfo=UTC)
    before_publication = pl.DataFrame(
        {
            "observation_date": [date(2026, 7, 31)],
            "meeting_date": [date(2026, 9, 16)],
            "move_bp": [25.0],
            "probability": [1.0],
            "available_at_utc": [available],
        }
    )
    with pytest.raises(ValueError, match="available"):
        build_fed_policy_features(
            before_publication.with_columns(
                pl.lit(datetime(2026, 8, 1, 12, tzinfo=UTC)).alias("available_at_utc")
            )
        )


def test_artifact_identity_is_stable():
    artifact = Path("artifacts/acceptance/fed-policy-cme-parity-v1.json").read_text()
    assert sha256(artifact.encode()).hexdigest() == (
        "2a58a91fb76ca4fc0f74ba36bc40715eed2f02c977c5f75170a7597ac11833e8"
    )
