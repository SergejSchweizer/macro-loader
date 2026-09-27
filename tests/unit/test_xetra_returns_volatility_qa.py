"""Independent mathematical QA for the XETRA-compatible return contract."""

from __future__ import annotations

import math
from statistics import stdev

LOG_WINDOWS = (1, 3, 5, 10, 20)
GEOMETRIC_WINDOWS = (5, 10, 20)
MEAN_WINDOWS = (5, 10, 20)
VOLATILITY_WINDOWS = (5, 10, 20, 40)


def _log_return(levels: list[float | None], index: int, lag: int) -> float | None:
    if index < lag:
        return None
    current = levels[index]
    previous = levels[index - lag]
    if current is None or previous is None or current <= 0 or previous <= 0:
        return None
    return math.log(current / previous)


def _complete_window(values: list[float | None], index: int, window: int) -> list[float] | None:
    if index < window - 1:
        return None
    result = values[index - window + 1 : index + 1]
    if any(value is None for value in result):
        return None
    return [value for value in result if value is not None]


def independent_return_features(levels: list[float | None]) -> dict[str, list[float | None]]:
    """Calculate the expected columns without importing production code or SQL."""

    result: dict[str, list[float | None]] = {}
    one_observation = [_log_return(levels, index, 1) for index in range(len(levels))]
    for lag in LOG_WINDOWS:
        result[f"log_return_{lag}"] = [
            _log_return(levels, index, lag) for index in range(len(levels))
        ]
    for window in GEOMETRIC_WINDOWS:
        values: list[float | None] = []
        for index in range(len(levels)):
            observations = _complete_window(one_observation, index, window)
            values.append(None if observations is None else math.exp(sum(observations)) - 1.0)
        result[f"return_geom_{window}_pct"] = values
    for window in MEAN_WINDOWS:
        values = []
        for index in range(len(levels)):
            observations = _complete_window(one_observation, index, window)
            values.append(None if observations is None else sum(observations) / window)
        result[f"return_mean_{window}"] = values
    for window in VOLATILITY_WINDOWS:
        values = []
        for index in range(len(levels)):
            observations = _complete_window(one_observation, index, window)
            values.append(None if observations is None else stdev(observations))
        result[f"volatility_{window}"] = values
    return result


def test_reference_covers_every_pr132_family_and_warmup() -> None:
    levels = [100.0 * 1.01**index for index in range(45)]
    result = independent_return_features(levels)

    assert set(result) == {
        *(f"log_return_{window}" for window in LOG_WINDOWS),
        *(f"return_geom_{window}_pct" for window in GEOMETRIC_WINDOWS),
        *(f"return_mean_{window}" for window in MEAN_WINDOWS),
        *(f"volatility_{window}" for window in VOLATILITY_WINDOWS),
    }
    assert result["return_geom_5_pct"][5] == 1.01**5 - 1.0
    assert result["return_geom_5_pct"][3] is None
    assert result["volatility_5"][5] is not None
    assert result["volatility_40"][40] is not None


def test_reference_preserves_decimal_pct_and_mean_log_semantics() -> None:
    levels = [100.0, 105.0, 110.0, 115.0, 120.0, 125.0]
    result = independent_return_features(levels)
    expected_logs = [math.log(levels[index] / levels[index - 1]) for index in range(1, 6)]

    assert math.isclose(result["return_geom_5_pct"][-1], levels[-1] / levels[0] - 1.0)
    assert result["return_geom_5_pct"][-1] < 1.0
    assert result["return_mean_5"][-1] == sum(expected_logs[-5:]) / 5
    assert result["volatility_5"][-1] == stdev(expected_logs[-5:])


def test_reference_is_causal_and_invalid_values_fail_closed() -> None:
    levels = [100.0, 101.0, 102.0, 0.0, 104.0, 105.0, 106.0]
    changed_future = [*levels[:5], 1000.0, 1001.0]
    original = independent_return_features(levels)
    changed = independent_return_features(changed_future)

    assert original["log_return_1"][:4] == changed["log_return_1"][:4]
    assert original["return_geom_5_pct"][3] is None
    assert original["return_geom_5_pct"][4] is None


def test_reference_detects_the_common_percentage_scaling_error() -> None:
    levels = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    expected = independent_return_features(levels)["return_geom_5_pct"][-1]
    altered_formula = expected * 100.0

    assert expected == levels[-1] / levels[0] - 1.0
    assert not math.isclose(expected, altered_formula)
