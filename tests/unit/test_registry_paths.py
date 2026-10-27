from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

from application.contracts import (
    BootstrapPolicy,
    FetchCapability,
    Frequency,
    NativeShape,
    Provider,
    SeriesContract,
    validate_series_registry,
)
from application.paths import LakePaths
from application.ports.provider_registry import AdapterRegistry
from application.registry import INITIAL_SERIES, SERIES_REGISTRY, series_contract


def _contract(**overrides: object) -> SeriesContract:
    values: dict[str, object] = {
        "series_id": "x",
        "provider": Provider.FRED,
        "source_id": "SOURCE",
        "unit": "percent",
        "native_shape": NativeShape.SCALAR,
        "frequency": Frequency.DAILY,
        "bootstrap_policy": BootstrapPolicy.MAXIMUM_EXPOSED_HISTORY,
        "fetch_capability": FetchCapability.DATE_RANGE,
    }
    values.update(overrides)
    return SeriesContract(**values)  # type: ignore[arg-type]


def test_initial_registry_contains_exact_canonical_series() -> None:
    assert tuple(SERIES_REGISTRY) == (
        "vix",
        "vix9d",
        "vix3m",
        "vix6m",
        "vix1y",
        "vstoxx",
        "vstoxx1m",
        "vstoxx2m",
        "vstoxx3m",
        "move",
        "ciss",
        "ciss_money",
        "ciss_bonds",
        "ciss_equity",
        "ciss_financials",
        "estr",
        "euro_hy_oas",
        "euro_hy_yield",
        "euro_hy_ytw",
        "euro_hy_total_return",
        "us_2y",
        "us_10y",
        "usd_broad",
        "us_3m",
        "us_5y",
    )
    assert len(INITIAL_SERIES) == 25
    assert all(entry.source_id and entry.unit for entry in INITIAL_SERIES)


def test_vstoxx_is_unambiguous_scalar_full_file() -> None:
    contract = series_contract("vstoxx")
    assert contract.provider is Provider.STOXX
    assert contract.native_shape is NativeShape.SCALAR
    assert contract.fetch_capability is FetchCapability.FULL_FILE


@pytest.mark.parametrize(
    ("series_id", "source_id"),
    (("vstoxx1m", "V6I1"), ("vstoxx2m", "V6I2"), ("vstoxx3m", "V6I3")),
)
def test_vstoxx_term_series_contracts_are_daily_scalar_full_files(
    series_id: str, source_id: str
) -> None:
    contract = series_contract(series_id)
    assert contract.provider is Provider.STOXX
    assert contract.source_id == source_id
    assert contract.unit == "index_points"
    assert contract.native_shape is NativeShape.SCALAR
    assert contract.frequency is Frequency.DAILY
    assert contract.bootstrap_policy is BootstrapPolicy.MAXIMUM_EXPOSED_HISTORY
    assert contract.fetch_capability is FetchCapability.FULL_FILE


@pytest.mark.parametrize(
    ("series_id", "source_id"),
    (
        ("ciss_money", "CISS.D.U2.Z0Z.4F.EC.SS_MMN.CON"),
        ("ciss_bonds", "CISS.D.U2.Z0Z.4F.EC.SS_BMN.CON"),
        ("ciss_equity", "CISS.D.U2.Z0Z.4F.EC.SS_EMN.CON"),
        ("ciss_financials", "CISS.D.U2.Z0Z.4F.EC.SS_FIN.CON"),
    ),
)
def test_ciss_components_are_daily_scalar_dimensionless_contributions(
    series_id: str, source_id: str
) -> None:
    contract = series_contract(series_id)
    assert contract.provider is Provider.ECB
    assert contract.source_id == source_id
    assert contract.unit == "dimensionless_contribution"
    assert contract.native_shape is NativeShape.SCALAR
    assert contract.frequency is Frequency.DAILY
    assert contract.bootstrap_policy is BootstrapPolicy.MAXIMUM_EXPOSED_HISTORY
    assert contract.fetch_capability is FetchCapability.DATE_RANGE


@pytest.mark.parametrize(
    ("series_id", "source_id", "unit"),
    (
        ("euro_hy_yield", "BAMLHE00EHYIEY", "percent"),
        ("euro_hy_ytw", "BAMLHE00EHYISYTW", "percent"),
        ("euro_hy_total_return", "BAMLHE00EHYITRIV", "index"),
    ),
)
def test_euro_hy_series_are_daily_fred_scalars_with_distinct_units(
    series_id: str, source_id: str, unit: str
) -> None:
    contract = series_contract(series_id)
    assert contract.provider is Provider.FRED
    assert contract.source_id == source_id
    assert contract.unit == unit
    assert contract.native_shape is NativeShape.SCALAR
    assert contract.frequency is Frequency.DAILY
    assert contract.bootstrap_policy is BootstrapPolicy.MAXIMUM_EXPOSED_HISTORY
    assert contract.fetch_capability is FetchCapability.DATE_RANGE


@pytest.mark.parametrize(
    ("series_id", "source_id"),
    (
        ("us_3m", "DGS3MO"),
        ("us_5y", "DGS5"),
    ),
)
def test_us_treasury_series_are_daily_fred_percent_scalars(series_id: str, source_id: str) -> None:
    contract = series_contract(series_id)
    assert contract.provider is Provider.FRED
    assert contract.source_id == source_id
    assert contract.unit == "percent"
    assert contract.native_shape is NativeShape.SCALAR
    assert contract.frequency is Frequency.DAILY
    assert contract.bootstrap_policy is BootstrapPolicy.MAXIMUM_EXPOSED_HISTORY
    assert contract.fetch_capability is FetchCapability.DATE_RANGE


def test_unknown_series_fails_explicitly() -> None:
    with pytest.raises(KeyError, match="unknown canonical series_id"):
        series_contract("missing")


def test_registry_rejects_duplicate_series() -> None:
    entry = _contract()
    with pytest.raises(ValueError, match="duplicate canonical"):
        validate_series_registry([entry, entry])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("series_id", "", "series_id"),
        ("source_id", " ", "source_id"),
        ("unit", "", "unit"),
        ("provider", "fred", "provider"),
        ("native_shape", "scalar", "native_shape"),
        ("frequency", "daily", "frequency"),
        ("bootstrap_policy", "maximum_exposed_history", "bootstrap_policy"),
        ("fetch_capability", "date_range", "fetch_capability"),
    ],
)
def test_series_contract_rejects_invalid_metadata(field: str, value: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _contract(**{field: value})


def test_lake_paths_match_documented_contract() -> None:
    paths = LakePaths(Path("lake"))
    day = date(2026, 8, 18)
    build = "20260818T020000Z"

    assert paths.bronze_month(Provider.CBOE, "vix", day) == Path(
        "lake/bronze/provider=cboe/series=vix/year=2026/month=08/data.parquet"
    )
    assert paths.silver_month("vix", day) == Path(
        "lake/silver/series=vix/year=2026/month=08/data.parquet"
    )
    assert paths.gold_build_root(build) == Path(
        "lake/gold/dataset=macro_features_daily/versions/build_id=20260818T020000Z"
    )
    assert paths.gold_data(build).name == "data.parquet"
    assert paths.gold_build_manifest(build).name == "manifest.json"
    assert paths.gold_build_profile(build).name == "feature_profile.png"
    assert paths.gold_manifest_parquet() == Path(
        "lake/gold/dataset=macro_features_daily/manifest.parquet"
    )
    assert paths.gold_manifest_json().name == "manifest.json"
    assert paths.gold_profile().name == "feature_profile.png"
    assert paths.ingestion_state() == Path("lake/state/ingestion_state.parquet")
    assert paths.ingestion_runs() == Path("lake/manifests/ingestion_runs.parquet")
    assert paths.inventory() == Path("lake/manifests/dataset_inventory.parquet")


def test_lake_paths_reject_invalid_build_id() -> None:
    with pytest.raises(ValueError, match="invalid Gold build_id"):
        LakePaths().gold_build_root("2026-08-18")


@dataclass(frozen=True)
class FakeAdapter:
    provider: Provider


def test_adapter_registry_resolves_without_provider_conditionals() -> None:
    cboe = FakeAdapter(Provider.CBOE)
    fred = FakeAdapter(Provider.FRED)
    registry = AdapterRegistry([cboe, fred])
    assert registry.resolve(Provider.CBOE) is cboe
    assert registry.resolve(Provider.FRED) is fred


def test_adapter_registry_rejects_duplicates_and_unknown_provider() -> None:
    with pytest.raises(ValueError, match="duplicate adapter"):
        AdapterRegistry([FakeAdapter(Provider.CBOE), FakeAdapter(Provider.CBOE)])
    registry = AdapterRegistry([FakeAdapter(Provider.CBOE)])
    with pytest.raises(KeyError, match="no adapter registered"):
        registry.resolve(Provider.FRED)
