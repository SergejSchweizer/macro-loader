from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from application.paths import LakePaths
from ingestion.fed_policy_reference_store import FedPolicyReferenceStore

NOW = datetime(2026, 1, 5, 18, tzinfo=UTC)


def _effr(value: float = 3.64, available: datetime = NOW) -> pl.DataFrame:
    return pl.DataFrame(
        [(date(2026, 1, 2), value, "fred-effr", "https://fred.example", available)],
        schema={
            "activity_date": pl.Date,
            "effr_percent": pl.Float64,
            "source_id": pl.String,
            "source_url": pl.String,
            "available_at_utc": pl.Datetime("us", "UTC"),
        },
        orient="row",
    )


def test_effr_visibility_is_cutoff_causal_and_revision_is_idempotent(tmp_path: Path) -> None:
    store = FedPolicyReferenceStore(LakePaths(tmp_path))
    store.upsert_effr(_effr())

    assert store.effr_as_of(datetime(2026, 1, 5, 17, tzinfo=UTC)).is_empty()
    assert store.effr_as_of(datetime(2026, 1, 5, 19, tzinfo=UTC))[0, "effr_percent"] == 3.64
    before = store.read_effr()
    store.upsert_effr(_effr())
    assert store.read_effr().equals(before)


def test_reference_families_preserve_causal_schedule_and_target_vintages(tmp_path: Path) -> None:
    store = FedPolicyReferenceStore(LakePaths(tmp_path))
    meeting = pl.DataFrame(
        [(date(2026, 3, 18), "scheduled", "fomc", "https://fed.example", NOW)],
        schema={
            "decision_date": pl.Date,
            "meeting_type": pl.String,
            "source_id": pl.String,
            "source_url": pl.String,
            "first_known_at_utc": pl.Datetime("us", "UTC"),
        },
        orient="row",
    )
    target = pl.DataFrame(
        [(date(2026, 1, 1), 3.25, 3.50, "fed-target", "https://fed.example", NOW)],
        schema={
            "effective_date": pl.Date,
            "lower_bound_percent": pl.Float64,
            "upper_bound_percent": pl.Float64,
            "source_id": pl.String,
            "source_url": pl.String,
            "known_at_utc": pl.Datetime("us", "UTC"),
        },
        orient="row",
    )
    store.upsert_meetings(meeting)
    store.upsert_targets(target)

    cutoff = datetime(2026, 1, 5, 19, tzinfo=UTC)
    assert store.meetings_as_of(cutoff).height == 1
    assert store.targets_as_of(cutoff)[0, "upper_bound_percent"] == 3.50


def test_invalid_target_range_fails_closed(tmp_path: Path) -> None:
    frame = pl.DataFrame(
        [(date(2026, 1, 1), 4.0, 3.0, "fed", "https://fed.example", NOW)],
        schema={
            "effective_date": pl.Date,
            "lower_bound_percent": pl.Float64,
            "upper_bound_percent": pl.Float64,
            "source_id": pl.String,
            "source_url": pl.String,
            "known_at_utc": pl.Datetime("us", "UTC"),
        },
        orient="row",
    )
    with pytest.raises(ValueError, match="lower bound"):
        FedPolicyReferenceStore(LakePaths(tmp_path)).upsert_targets(frame)
