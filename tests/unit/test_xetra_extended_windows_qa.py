"""Independent mathematical QA for PR-170's scalar-level feature windows.

This file intentionally does not import application code, production feature
constants, catalog builders, or SQL-expression helpers. The tuples below are a
frozen copy of the consumer-facing requirement, not runtime configuration.
"""

from __future__ import annotations

import math
from statistics import stdev

LOG_WINDOWS = (1, 2, 3, 4, 5, 8, 10, 20)
GEOMETRIC_WINDOWS = (4, 5, 8, 10, 16, 20)
MEAN_WINDOWS = (5, 10, 20)
VOLATILITY_WINDOWS = (5, 8, 10, 20, 32, 40, 96)
SMA_RATIOS = ((5, 15), (5, 20), (10, 20), (10, 40))
RSI_WINDOWS = (3, 5, 7, 14)
ROC_WINDOWS = (1, 2, 3, 4, 5, 8, 10, 20)
DRAWDOWN_WINDOWS = (20, 32, 60, 96)


def _positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0.0


def _window(
    values: list[float | None], index: int, size: int, *, positive_only: bool = False
) -> list[float] | None:
    if index + 1 < size:
        return None
    result = values[index - size + 1 : index + 1]
    if any(value is None for value in result):
        return None
    concrete = [value for value in result if value is not None]
    if positive_only and any(not _positive(value) for value in concrete):
        return None
    if any(not math.isfinite(value) for value in concrete):
        return None
    return concrete


def _wilder_rsi(levels: list[float | None], period: int) -> list[float | None]:
    """Wilder smoothing with a simple-mean seed and reseeding after bad pairs."""
    output: list[float | None] = [None] * len(levels)
    seed_gains: list[float] = []
    seed_losses: list[float] = []
    average_gain: float | None = None
    average_loss: float | None = None

    for index in range(1, len(levels)):
        previous, current = levels[index - 1], levels[index]
        if not _positive(previous) or not _positive(current):
            seed_gains.clear()
            seed_losses.clear()
            average_gain = average_loss = None
            continue

        assert previous is not None and current is not None
        gain = max(current - previous, 0.0)
        loss = max(previous - current, 0.0)
        if average_gain is None or average_loss is None:
            seed_gains.append(gain)
            seed_losses.append(loss)
            if len(seed_gains) < period:
                continue
            average_gain = sum(seed_gains) / period
            average_loss = sum(seed_losses) / period
        else:
            average_gain = ((period - 1) * average_gain + gain) / period
            average_loss = ((period - 1) * average_loss + loss) / period

        if index >= period:
            if average_gain == 0.0 and average_loss == 0.0:
                output[index] = None
            elif average_loss == 0.0:
                output[index] = 100.0
            else:
                output[index] = 100.0 - 100.0 / (1.0 + average_gain / average_loss)
    return output


def independent_level_features(levels: list[float | None]) -> dict[str, list[float | None]]:
    """Compute required feature families from scalar levels, without SQL/code reuse."""
    result: dict[str, list[float | None]] = {}
    one_step_logs: list[float | None] = [None]
    for previous, current in zip(levels[:-1], levels[1:], strict=True):
        one_step_logs.append(
            math.log(current / previous) if _positive(previous) and _positive(current) else None
        )

    for lag in LOG_WINDOWS:
        result[f"log_return_{lag}"] = [
            math.log(current / levels[index - lag])
            if index >= lag and _positive(current) and _positive(levels[index - lag])
            else None
            for index, current in enumerate(levels)
        ]

    for window in GEOMETRIC_WINDOWS:
        observations: list[float | None] = []
        for index in range(len(levels)):
            returns = _window(one_step_logs, index, window)
            observations.append(None if returns is None else math.exp(sum(returns)) - 1.0)
        result[f"return_geom_{window}_pct"] = observations

    for window in MEAN_WINDOWS:
        observations = []
        for index in range(len(levels)):
            returns = _window(one_step_logs, index, window)
            observations.append(None if returns is None else sum(returns) / window)
        result[f"return_mean_{window}"] = observations

    for window in VOLATILITY_WINDOWS:
        observations = []
        for index in range(len(levels)):
            returns = _window(one_step_logs, index, window)
            observations.append(None if returns is None else stdev(returns))
        result[f"volatility_{window}"] = observations

    for short, long in SMA_RATIOS:
        observations = []
        for index in range(len(levels)):
            short_values = _window(levels, index, short, positive_only=True)
            long_values = _window(levels, index, long, positive_only=True)
            if short_values is None or long_values is None:
                observations.append(None)
            else:
                denominator = sum(long_values) / long
                observations.append(
                    None if denominator == 0.0 else (sum(short_values) / short) / denominator
                )
        result[f"sma_ratio_{short}_{long}"] = observations

    for period in RSI_WINDOWS:
        result[f"rsi_{period}"] = _wilder_rsi(levels, period)

    for lag in ROC_WINDOWS:
        result[f"roc_{lag}"] = [
            levels[index] / levels[index - lag] - 1.0
            if index >= lag and _positive(levels[index]) and _positive(levels[index - lag])
            else None
            for index in range(len(levels))
        ]

    for window in DRAWDOWN_WINDOWS:
        observations = []
        for index, current in enumerate(levels):
            values = _window(levels, index, window, positive_only=True)
            observations.append(
                None if values is None or not _positive(current) else current / max(values) - 1.0
            )
        result[f"drawdown_{window}"] = observations
    return result


def _ordinary_levels(size: int) -> list[float]:
    """Mixed, hand-auditable changes exercise both positive and negative returns."""
    return [100.0 + index * 0.7 + ((index % 7) - 3) * 1.1 for index in range(size)]


def test_independent_reference_covers_exact_old_and_extended_windows() -> None:
    values = independent_level_features(_ordinary_levels(140))
    assert set(values) == {
        *(f"log_return_{window}" for window in LOG_WINDOWS),
        *(f"return_geom_{window}_pct" for window in GEOMETRIC_WINDOWS),
        *(f"return_mean_{window}" for window in MEAN_WINDOWS),
        *(f"volatility_{window}" for window in VOLATILITY_WINDOWS),
        *(f"sma_ratio_{short}_{long}" for short, long in SMA_RATIOS),
        *(f"rsi_{window}" for window in RSI_WINDOWS),
        *(f"roc_{window}" for window in ROC_WINDOWS),
        *(f"drawdown_{window}" for window in DRAWDOWN_WINDOWS),
    }
    for key, observations in values.items():
        assert len(observations) == 140, key


def test_warmup_and_hand_calculable_values_for_each_extended_family() -> None:
    levels = _ordinary_levels(140)
    values = independent_level_features(levels)

    assert values["log_return_8"][7] is None
    assert values["log_return_8"][8] is not None
    assert values["return_geom_16_pct"][15] is None
    assert values["return_geom_16_pct"][16] is not None
    assert values["volatility_96"][95] is None
    assert values["volatility_96"][96] is not None
    assert values["sma_ratio_5_15"][13] is None
    assert values["sma_ratio_5_15"][14] is not None
    assert values["rsi_3"][2] is None
    assert values["rsi_3"][3] is not None
    assert values["roc_8"][7] is None
    assert values["roc_8"][8] is not None
    assert values["drawdown_96"][94] is None
    assert values["drawdown_96"][95] is not None

    last = len(levels) - 1
    assert math.isclose(values["log_return_8"][last], math.log(levels[-1] / levels[-9]))
    assert math.isclose(values["roc_8"][last], levels[-1] / levels[-9] - 1.0)
    assert math.isclose(
        values["sma_ratio_5_15"][last],
        (sum(levels[-5:]) / 5.0) / (sum(levels[-15:]) / 15.0),
    )
    assert math.isclose(values["drawdown_96"][last], levels[-1] / max(levels[-96:]) - 1.0)


def test_flat_series_invalid_levels_short_history_and_decimal_pct() -> None:
    flat = independent_level_features([80.0] * 100)
    assert flat["log_return_8"][-1] == 0.0
    assert flat["return_geom_16_pct"][-1] == 0.0
    assert flat["volatility_96"][-1] == 0.0
    assert flat["sma_ratio_5_15"][-1] == 1.0
    for period in RSI_WINDOWS:
        assert flat[f"rsi_{period}"][-1] is None

    rising = independent_level_features([float(100 + index) for index in range(100)])
    falling = independent_level_features([float(200 - index) for index in range(100)])
    for period in RSI_WINDOWS:
        assert rising[f"rsi_{period}"][-1] == 100.0
        assert falling[f"rsi_{period}"][-1] == 0.0
    assert flat["roc_8"][-1] == 0.0
    assert flat["drawdown_96"][-1] == 0.0

    short = independent_level_features([100.0, 101.0, 102.0, 103.0])
    assert short["volatility_5"][-1] is None
    assert short["sma_ratio_5_15"][-1] is None
    assert short["rsi_5"][-1] is None
    assert short["drawdown_20"][-1] is None

    with_invalid: list[float | None] = [100.0 + index for index in range(110)]
    with_invalid[45] = 0.0
    with_invalid[73] = None
    with_invalid[88] = -5.0
    invalid = independent_level_features(with_invalid)
    for family in (
        "return_geom_8_pct",
        "return_mean_5",
        "volatility_8",
        "sma_ratio_5_15",
        "drawdown_20",
    ):
        assert invalid[family][45] is None, family
    assert invalid["rsi_3"][45] is None
    assert invalid["rsi_3"][49] is not None  # valid transitions reseed Wilder averages
    assert invalid["roc_8"][45] is None

    ramp = [100.0, 105.0, 110.0, 115.0, 120.0, 125.0]
    geometric = independent_level_features(ramp)["return_geom_5_pct"][-1]
    assert geometric is not None
    assert math.isclose(geometric, ramp[-1] / ramp[0] - 1.0)
    assert not math.isclose(geometric, (ramp[-1] / ramp[0] - 1.0) * 100.0)


def test_all_independent_families_are_causal_under_future_extension_and_truncation() -> None:
    prefix = _ordinary_levels(110)
    extended = [*prefix, 9_999.0, 0.01, 50_000.0]
    expected_prefix = independent_level_features(prefix)
    full = independent_level_features(extended)
    for name, observations in expected_prefix.items():
        assert full[name][: len(prefix)] == observations, name
