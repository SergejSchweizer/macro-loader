"""Recoverable root materialized views derived only from authoritative Gold catalog state."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from application.gold_catalog import GoldCatalogRecord
from application.paths import LakePaths

FaultInjector = Callable[[str], None]
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
logger = logging.getLogger(__name__)


def _no_fault(stage: str) -> None:
    del stage


def _utc_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("Gold catalog materialized-view datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _catalog_json(record: GoldCatalogRecord) -> bytes:
    payload = {
        "build_id": record.build_id,
        "build_manifest_path": record.build_manifest_path,
        "completed_at_utc": _utc_text(record.completed_at_utc),
        "current": record.current,
        "data_path": record.data_path,
        "dataset_id": record.dataset_id,
        "feature_version": record.feature_version,
        "max_timestamp": _utc_text(record.max_timestamp),
        "min_timestamp": _utc_text(record.min_timestamp),
        "plot_path": record.plot_path,
        "pruned_at_utc": _utc_text(record.pruned_at_utc),
        "row_count": record.row_count,
        "schema_version": record.schema_version,
        "started_at_utc": _utc_text(record.started_at_utc),
        "status": record.status.value,
    }
    return (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _atomic_replace_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


class GoldMaterializedViewWriter:
    """Materialized View adapter; root JSON/PNG are never publication authority."""

    def __init__(
        self,
        paths: LakePaths,
        *,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self._paths = paths
        self._fault = fault_injector if fault_injector is not None else _no_fault

    def refresh(self, records: Sequence[GoldCatalogRecord]) -> None:
        logger.debug("gold root views refresh started catalog_rows=%d", len(records))
        try:
            current = [record for record in records if record.current]
            if len(current) > 1:
                raise ValueError("Gold catalog contains multiple current rows")
            if not current:
                self._paths.gold_manifest_json().unlink(missing_ok=True)
                self._paths.gold_profile().unlink(missing_ok=True)
                self._fault("after_clear")
                logger.debug("gold root views cleared")
                return

            record = current[0]
            if not record.selectable_complete:
                raise ValueError("Gold current row is not a selectable complete build")
            data_path, manifest_path, plot_path = self._physical_paths(record)
            if not data_path.is_file() or not manifest_path.is_file() or not plot_path.is_file():
                raise FileNotFoundError("Gold current bundle is physically incomplete")
            plot_bytes = plot_path.read_bytes()
            if not plot_bytes.startswith(_PNG_SIGNATURE):
                raise ValueError("Gold current feature profile is not a PNG")
            root_json = _catalog_json(record)

            self._fault("before_root_json")
            _atomic_replace_bytes(self._paths.gold_manifest_json(), root_json)
            self._fault("after_root_json")
            _atomic_replace_bytes(self._paths.gold_profile(), plot_bytes)
            self._fault("after_root_profile")
        except Exception as exc:
            logger.debug(
                "gold root views refresh failed error_type=%s",
                type(exc).__name__,
            )
            raise
        logger.debug("gold root views refreshed build_id=%s", record.build_id)

    def _physical_paths(self, record: GoldCatalogRecord) -> tuple[Path, Path, Path]:
        build_id = record.build_id
        data_rel = f"versions/build_id={build_id}/data.parquet"
        manifest_rel = f"versions/build_id={build_id}/manifest.json"
        plot_rel = f"versions/build_id={build_id}/feature_profile.png"
        expected_rel = (data_rel, manifest_rel, plot_rel)
        actual_rel = (record.data_path, record.build_manifest_path, record.plot_path)
        if actual_rel != expected_rel:
            raise ValueError("Gold current catalog artifact path shape mismatch")
        root = self._paths.gold_dataset_root()
        physical = (root / data_rel, root / manifest_rel, root / plot_rel)
        build_root = self._paths.gold_build_root(build_id)
        if any(path.parent != build_root for path in physical):
            raise ValueError("Gold current artifact escapes expected build directory")
        return physical
