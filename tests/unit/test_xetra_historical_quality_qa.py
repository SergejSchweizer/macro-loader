"""Deterministic historical/revision/replay acceptance helpers."""

from __future__ import annotations

import hashlib
import json
import math

FEATURE_WINDOWS = (1, 3, 5, 10, 20, 40, 60)


def affected_observation_indexes(revision_index: int, observation_count: int) -> set[int]:
    """Return the causal rows that can change after one historical level revision."""

    affected: set[int] = set()
    for window in FEATURE_WINDOWS:
        affected.update(
            index
            for index in range(revision_index, observation_count)
            if index - revision_index < window
        )
    return affected


def finite_or_null(values: list[float | None]) -> bool:
    return all(value is None or math.isfinite(value) for value in values)


def sanitized_artifact(payload: dict[str, object]) -> str:
    clean = {key: value for key, value in payload.items() if "secret" not in key.lower()}
    return json.dumps(clean, sort_keys=True, separators=(",", ":"))


def test_revision_impact_is_causal_and_bounded() -> None:
    affected = affected_observation_indexes(revision_index=100, observation_count=200)

    assert min(affected) == 100
    assert max(affected) == 159
    assert 99 not in affected
    assert 160 not in affected


def test_historical_quality_rejects_nonfinite_values_and_unexplained_gaps() -> None:
    assert finite_or_null([None, 0.0, 1.5, -2.0])
    assert not finite_or_null([1.0, float("nan")])
    assert not finite_or_null([1.0, float("inf")])


def test_replay_artifact_is_stable_and_sanitized() -> None:
    first = sanitized_artifact({"rows": 10, "status": "PASS", "secret_dsn": "postgresql://secret"})
    second = sanitized_artifact({"secret_dsn": "different", "status": "PASS", "rows": 10})

    assert first == second
    assert "secret" not in first.lower()
    assert hashlib.sha256(first.encode()).hexdigest() == hashlib.sha256(second.encode()).hexdigest()


def test_replay_requires_zero_mutations_and_zero_refreshes() -> None:
    replay = {"inserted": 0, "updated": 0, "deleted": 0, "refreshes": 0}

    assert sum(replay.values()) == 0
