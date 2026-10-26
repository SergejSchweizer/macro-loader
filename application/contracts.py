"""Canonical source-series contracts shared by application and adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class Provider(StrEnum):
    """Supported market-data providers."""

    CBOE = "cboe"
    STOXX = "stoxx"
    YAHOO = "yahoo"
    ECB = "ecb"
    FRED = "fred"


class NativeShape(StrEnum):
    """Provider-native observation shape retained in Bronze."""

    OHLC = "ohlc"
    SCALAR = "scalar"


class FetchCapability(StrEnum):
    """Provider history-fetch capability."""

    DATE_RANGE = "date_range"
    FULL_FILE = "full_file"


class Frequency(StrEnum):
    """Canonical source frequency."""

    DAILY = "daily"


class BootstrapPolicy(StrEnum):
    """Initial-history acquisition policy."""

    MAXIMUM_EXPOSED_HISTORY = "maximum_exposed_history"


@dataclass(frozen=True, slots=True)
class SeriesContract:
    """Immutable metadata contract for one canonical source series."""

    series_id: str
    provider: Provider
    source_id: str
    unit: str
    native_shape: NativeShape
    frequency: Frequency
    bootstrap_policy: BootstrapPolicy
    fetch_capability: FetchCapability

    def __post_init__(self) -> None:
        if not self.series_id or not self.series_id.strip():
            raise ValueError("series_id must be non-empty")
        if not isinstance(self.provider, Provider):
            raise ValueError("provider must be a supported Provider")
        if not self.source_id or not self.source_id.strip():
            raise ValueError("source_id must be non-empty")
        if not self.unit or not self.unit.strip():
            raise ValueError("unit must be non-empty")
        if not isinstance(self.native_shape, NativeShape):
            raise ValueError("native_shape must be a supported NativeShape")
        if not isinstance(self.frequency, Frequency):
            raise ValueError("frequency must be a supported Frequency")
        if not isinstance(self.bootstrap_policy, BootstrapPolicy):
            raise ValueError("bootstrap_policy must be a supported BootstrapPolicy")
        if not isinstance(self.fetch_capability, FetchCapability):
            raise ValueError("fetch_capability must be a supported FetchCapability")


def validate_series_registry(entries: Sequence[SeriesContract]) -> Mapping[str, SeriesContract]:
    """Validate canonical identities and return a read-only registry."""
    by_id: dict[str, SeriesContract] = {}
    for entry in entries:
        if entry.series_id in by_id:
            raise ValueError(f"duplicate canonical series_id: {entry.series_id}")
        by_id[entry.series_id] = entry
    return MappingProxyType(by_id)
