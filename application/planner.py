"""Pure bootstrap/update/reconcile planning for source ingestion."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from application.contracts import FetchCapability, SeriesContract
from application.ports.lake import ObservationBounds

logger = logging.getLogger(__name__)


class OperationMode(StrEnum):
    """Explicit ingestion operations."""

    BOOTSTRAP = "bootstrap"
    UPDATE = "update"
    RECONCILE = "reconcile"


@dataclass(frozen=True, slots=True)
class FetchInstruction:
    """Provider-neutral fetch instruction with a logical acceptance window."""

    mode: OperationMode
    request_start: date | None
    request_end: date
    maximum_history: bool
    filter_start: date | None
    filter_end: date | None


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    """Strict incremental update configuration."""

    overlap_days: int = 7

    def __post_init__(self) -> None:
        if self.overlap_days < 0:
            raise ValueError("overlap_days must be >= 0")


def choose_mode(bounds: ObservationBounds, *, explicit_reconcile: bool = False) -> OperationMode:
    """Choose mode solely from authoritative history and explicit operator intent."""
    if bounds.maximum is None:
        if explicit_reconcile:
            raise ValueError("cannot reconcile a series with no Bronze history")
        return OperationMode.BOOTSTRAP
    if explicit_reconcile:
        return OperationMode.RECONCILE
    return OperationMode.UPDATE


def build_plan(
    contract: SeriesContract,
    bounds: ObservationBounds,
    *,
    today: date,
    explicit_reconcile: bool = False,
    config: PlannerConfig | None = None,
) -> FetchInstruction:
    """Build a deterministic provider-neutral plan from authoritative Bronze bounds."""
    logger.debug(
        "source fetch planning started provider=%s series_id=%s explicit_reconcile=%s",
        contract.provider,
        contract.series_id,
        explicit_reconcile,
    )
    try:
        instruction = _build_plan(
            contract,
            bounds,
            today=today,
            explicit_reconcile=explicit_reconcile,
            config=config,
        )
    except Exception as exc:
        logger.debug(
            "source fetch planning failed provider=%s series_id=%s error_type=%s",
            contract.provider,
            contract.series_id,
            type(exc).__name__,
        )
        raise
    logger.debug(
        "source fetch planning completed provider=%s series_id=%s mode=%s "
        "fetch_capability=%s request_start=%s request_end=%s maximum_history=%s "
        "filter_start=%s filter_end=%s",
        contract.provider,
        contract.series_id,
        instruction.mode.value,
        contract.fetch_capability.value,
        instruction.request_start,
        instruction.request_end,
        instruction.maximum_history,
        instruction.filter_start,
        instruction.filter_end,
    )
    return instruction


def _build_plan(
    contract: SeriesContract,
    bounds: ObservationBounds,
    *,
    today: date,
    explicit_reconcile: bool,
    config: PlannerConfig | None,
) -> FetchInstruction:
    """Construct the plan; the public function owns its safe logging lifecycle."""
    effective_config = config if config is not None else PlannerConfig()
    mode = choose_mode(bounds, explicit_reconcile=explicit_reconcile)
    latest = bounds.maximum

    if mode is OperationMode.BOOTSTRAP:
        return FetchInstruction(
            mode=mode,
            request_start=None,
            request_end=today,
            maximum_history=True,
            filter_start=None,
            filter_end=None,
        )

    if latest is None:
        raise AssertionError("existing-history mode requires authoritative Bronze maximum")
    if today < latest:
        raise ValueError("today cannot be earlier than latest stored observation")

    if mode is OperationMode.RECONCILE:
        return FetchInstruction(
            mode=mode,
            request_start=None,
            request_end=today,
            maximum_history=True,
            filter_start=None,
            filter_end=None,
        )

    request_start = latest - timedelta(days=effective_config.overlap_days)
    if contract.fetch_capability is FetchCapability.DATE_RANGE:
        return FetchInstruction(
            mode=mode,
            request_start=request_start,
            request_end=today,
            maximum_history=False,
            filter_start=request_start,
            filter_end=today,
        )
    if contract.fetch_capability is FetchCapability.FULL_FILE:
        return FetchInstruction(
            mode=mode,
            request_start=None,
            request_end=today,
            maximum_history=False,
            filter_start=request_start,
            filter_end=today,
        )
    raise ValueError(f"unsupported fetch capability: {contract.fetch_capability}")
