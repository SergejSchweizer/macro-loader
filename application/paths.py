"""Single source of truth for medallion lake paths."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from application.contracts import Provider

_BUILD_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")
_GOLD_DATASET = "macro_features_daily"


@dataclass(frozen=True, slots=True)
class LakePaths:
    """Typed deterministic path service for all persisted contracts."""

    root: Path = Path("lake")

    def bronze_month(self, provider: Provider, series_id: str, observation_date: date) -> Path:
        return (
            self.root
            / "bronze"
            / f"provider={provider.value}"
            / f"series={series_id}"
            / f"year={observation_date.year:04d}"
            / f"month={observation_date.month:02d}"
            / "data.parquet"
        )

    def silver_month(self, series_id: str, observation_date: date) -> Path:
        return (
            self.root
            / "silver"
            / f"series={series_id}"
            / f"year={observation_date.year:04d}"
            / f"month={observation_date.month:02d}"
            / "data.parquet"
        )

    def gold_dataset_root(self) -> Path:
        return self.root / "gold" / f"dataset={_GOLD_DATASET}"

    def gold_build_root(self, build_id: str) -> Path:
        if _BUILD_ID_RE.fullmatch(build_id) is None:
            raise ValueError(f"invalid Gold build_id: {build_id}")
        return self.gold_dataset_root() / "versions" / f"build_id={build_id}"

    def gold_data(self, build_id: str) -> Path:
        return self.gold_build_root(build_id) / "data.parquet"

    def gold_build_manifest(self, build_id: str) -> Path:
        return self.gold_build_root(build_id) / "manifest.json"

    def gold_build_profile(self, build_id: str) -> Path:
        return self.gold_build_root(build_id) / "feature_profile.png"

    def gold_manifest_parquet(self) -> Path:
        return self.gold_dataset_root() / "manifest.parquet"

    def gold_manifest_json(self) -> Path:
        return self.gold_dataset_root() / "manifest.json"

    def gold_profile(self) -> Path:
        return self.gold_dataset_root() / "feature_profile.png"

    def ingestion_state(self) -> Path:
        return self.root / "state" / "ingestion_state.parquet"

    def ingestion_runs(self) -> Path:
        return self.root / "manifests" / "ingestion_runs.parquet"

    def inventory(self) -> Path:
        return self.root / "manifests" / "dataset_inventory.parquet"
