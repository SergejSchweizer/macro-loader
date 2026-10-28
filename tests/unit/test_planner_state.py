from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from application.contracts import FetchCapability
from application.planner import OperationMode, PlannerConfig, build_plan, choose_mode
from application.ports.lake import ObservationBounds
from application.registry import series_contract
from application.state import IngestionState, advance_state
from ingestion.state_repository import read_states, upsert_state, write_states

TODAY = date(2026, 8, 19)
LATEST = date(2026, 8, 18)
BOUNDS = ObservationBounds(date(2000, 1, 3), LATEST)


def test_mode_is_bootstrap_update_or_explicit_reconcile_only() -> None:
    assert choose_mode(ObservationBounds(None, None)) is OperationMode.BOOTSTRAP
    assert choose_mode(BOUNDS) is OperationMode.UPDATE
    assert choose_mode(BOUNDS, explicit_reconcile=True) is OperationMode.RECONCILE
    with pytest.raises(ValueError, match="cannot reconcile"):
        choose_mode(ObservationBounds(None, None), explicit_reconcile=True)


def test_canonical_delta_uses_latest_not_historical_minimum() -> None:
    plan = build_plan(series_contract("us_10y"), BOUNDS, today=TODAY)
    assert plan.mode is OperationMode.UPDATE
    assert plan.request_start == date(2026, 8, 11)
    assert plan.request_end == TODAY
    assert plan.filter_start == date(2026, 8, 11)
    assert plan.filter_end == TODAY
    assert not plan.maximum_history
    assert plan.request_start != BOUNDS.minimum


def test_full_file_update_keeps_exact_logical_filter_window() -> None:
    contract = series_contract("vix")
    assert contract.fetch_capability is FetchCapability.FULL_FILE
    plan = build_plan(contract, BOUNDS, today=TODAY)
    assert plan.request_start is None
    assert plan.filter_start == date(2026, 8, 11)
    assert plan.filter_end == TODAY
    assert not plan.maximum_history


def test_bootstrap_and_reconcile_are_maximum_history_and_explicit() -> None:
    bootstrap = build_plan(series_contract("us_10y"), ObservationBounds(None, None), today=TODAY)
    reconcile = build_plan(series_contract("us_10y"), BOUNDS, today=TODAY, explicit_reconcile=True)
    assert bootstrap.mode is OperationMode.BOOTSTRAP
    assert bootstrap.maximum_history
    assert bootstrap.request_start is None
    assert reconcile.mode is OperationMode.RECONCILE
    assert reconcile.maximum_history
    assert reconcile.request_start is None


def test_planner_validates_overlap_and_reverse_time() -> None:
    with pytest.raises(ValueError, match="overlap_days"):
        PlannerConfig(overlap_days=-1)
    with pytest.raises(ValueError, match="earlier"):
        build_plan(series_contract("us_10y"), BOUNDS, today=date(2026, 8, 17))


def test_planner_logs_safe_delta_plan_lifecycle(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="application.planner")

    plan = build_plan(series_contract("us_10y"), BOUNDS, today=TODAY)

    assert plan.request_start == date(2026, 8, 11)
    assert "source fetch planning started provider=fred series_id=us_10y" in caplog.text
    assert (
        "source fetch planning completed provider=fred series_id=us_10y mode=update "
        "fetch_capability=date_range request_start=2026-08-11 request_end=2026-08-19 "
        "maximum_history=False filter_start=2026-08-11 filter_end=2026-08-19"
    ) in caplog.text
    assert "2000-01-03" not in caplog.text


def test_planner_logs_error_class_without_exception_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="application.planner")

    with pytest.raises(ValueError, match="today cannot be earlier"):
        build_plan(series_contract("us_10y"), BOUNDS, today=date(2026, 8, 17))

    assert (
        "source fetch planning failed provider=fred series_id=us_10y error_type=ValueError"
        in caplog.text
    )
    assert "today cannot be earlier" not in caplog.text


def test_state_cache_never_overrides_authoritative_bronze_max() -> None:
    stale = IngestionState(
        provider=series_contract("us_10y").provider,
        series_id="us_10y",
        last_observed_date=date(2000, 1, 3),
    )
    assert stale.authoritative_latest(BOUNDS) == LATEST
    empty_cache = IngestionState(stale.provider, stale.series_id)
    assert empty_cache.authoritative_latest(BOUNDS) == LATEST
    impossible = IngestionState(stale.provider, stale.series_id, last_observed_date=LATEST)
    with pytest.raises(ValueError, match="authoritative Bronze is empty"):
        impossible.authoritative_latest(ObservationBounds(None, None))


def test_state_advances_only_after_both_durability_barriers() -> None:
    contract = series_contract("us_10y")
    prior = IngestionState(contract.provider, contract.series_id)
    plan = build_plan(contract, BOUNDS, today=TODAY)
    committed = datetime(2026, 8, 19, 2, tzinfo=UTC)
    assert (
        advance_state(
            prior,
            plan,
            committed_at_utc=committed,
            authoritative_bounds=BOUNDS,
            fetched_rows=3,
            accepted_rows=2,
            changed_rows=1,
            durable_bronze=False,
            durable_success_manifest=True,
        )
        == prior
    )
    assert (
        advance_state(
            prior,
            plan,
            committed_at_utc=committed,
            authoritative_bounds=BOUNDS,
            fetched_rows=3,
            accepted_rows=2,
            changed_rows=1,
            durable_bronze=True,
            durable_success_manifest=False,
        )
        == prior
    )
    advanced = advance_state(
        prior,
        plan,
        committed_at_utc=committed,
        authoritative_bounds=BOUNDS,
        fetched_rows=3,
        accepted_rows=2,
        changed_rows=1,
        durable_bronze=True,
        durable_success_manifest=True,
    )
    assert advanced.last_observed_date == LATEST
    assert advanced.last_requested_start == date(2026, 8, 11)
    assert advanced.mode is OperationMode.UPDATE
    assert advanced.last_reconcile_utc is None


def test_reconcile_timestamp_changes_only_after_explicit_success() -> None:
    contract = series_contract("us_10y")
    previous_reconcile = datetime(2026, 8, 1, tzinfo=UTC)
    prior = IngestionState(
        contract.provider, contract.series_id, last_reconcile_utc=previous_reconcile
    )
    update = build_plan(contract, BOUNDS, today=TODAY)
    committed = datetime(2026, 8, 19, 2, tzinfo=UTC)
    updated = advance_state(
        prior,
        update,
        committed_at_utc=committed,
        authoritative_bounds=BOUNDS,
        fetched_rows=0,
        accepted_rows=0,
        changed_rows=0,
        durable_bronze=True,
        durable_success_manifest=True,
    )
    assert updated.last_reconcile_utc == previous_reconcile
    reconcile = build_plan(contract, BOUNDS, today=TODAY, explicit_reconcile=True)
    reconciled = advance_state(
        updated,
        reconcile,
        committed_at_utc=committed,
        authoritative_bounds=BOUNDS,
        fetched_rows=100,
        accepted_rows=100,
        changed_rows=1,
        durable_bronze=True,
        durable_success_manifest=True,
    )
    assert reconciled.last_reconcile_utc == committed


def test_state_rejects_negative_counts() -> None:
    contract = series_contract("us_10y")
    prior = IngestionState(contract.provider, contract.series_id)
    plan = build_plan(contract, BOUNDS, today=TODAY)
    with pytest.raises(ValueError, match="fetched_rows"):
        advance_state(
            prior,
            plan,
            committed_at_utc=datetime(2026, 8, 19, tzinfo=UTC),
            authoritative_bounds=BOUNDS,
            fetched_rows=-1,
            accepted_rows=0,
            changed_rows=0,
            durable_bronze=True,
            durable_success_manifest=True,
        )


def test_state_repository_roundtrip_and_upsert(tmp_path: Path) -> None:
    path = tmp_path / "state.parquet"
    assert read_states(path) == []
    fred = IngestionState(series_contract("us_10y").provider, "us_10y")
    cboe = IngestionState(series_contract("vix").provider, "vix")
    write_states(path, [fred, cboe])
    states = read_states(path)
    assert [(item.provider.value, item.series_id) for item in states] == [
        ("cboe", "vix"),
        ("fred", "us_10y"),
    ]
    changed = IngestionState(
        fred.provider,
        fred.series_id,
        last_observed_date=LATEST,
        mode=OperationMode.UPDATE,
    )
    upsert_state(path, changed)
    reread = {item.series_id: item for item in read_states(path)}
    assert len(reread) == 2
    assert reread["us_10y"].last_observed_date == LATEST


def test_state_repository_rejects_duplicate_and_bad_schema(tmp_path: Path) -> None:
    path = tmp_path / "state.parquet"
    state = IngestionState(series_contract("us_10y").provider, "us_10y")
    with pytest.raises(ValueError, match="duplicate"):
        write_states(path, [state, state])
    pl.DataFrame({"bad": [1]}).write_parquet(path)
    with pytest.raises(ValueError, match="invalid ingestion-state schema"):
        read_states(path)


def test_state_repository_logs_identity_and_record_counts_only(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "state.parquet"
    state = IngestionState(series_contract("us_10y").provider, "us_10y")
    caplog.set_level(logging.DEBUG, logger="ingestion.state_repository")

    upsert_state(path, state)
    assert read_states(path) == [state]

    assert "ingestion state upsert started provider=fred series_id=us_10y" in caplog.text
    assert "ingestion state write completed record_count=1" in caplog.text
    assert "ingestion state upsert completed provider=fred series_id=us_10y" in caplog.text
    assert "ingestion state read completed record_count=1" in caplog.text
    assert str(tmp_path) not in caplog.text
