"""Independent reference QA for PR-171 composite/statistical view features.

The reference below is deliberately self-contained: it does not import the
application feature catalog, SQL builder, constants, or calculation code.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import stdev

from application.gold_frame import GOLD_COLUMNS
from application.macro_feature_catalog import FEATURE_COLUMNS, PRICE_FEATURE_SERIES
from application.postgres_sync import POSTGRES_RAW_COLUMNS

_EXPECTED_NEW_FEATURE_SUFFIXES = (
    "rsi_ratio_5_15",
    "rsi5_slope_3",
    "rsi5_delta_1",
    "roc_acceleration_4_8",
    "volatility_zscore_96",
    "level_zscore_96",
    "momentum_autocorr_lag1_window96",
    "momentum_autocorr_lag4_window96",
    "momentum_autocorr_lag8_window192",
)


def _is_positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0.0


def _wilder_rsi(levels: Sequence[float | None], period: int) -> list[float | None]:
    """Simple-mean seed, Wilder recurrence, and reset across invalid changes."""
    output: list[float | None] = [None] * len(levels)
    seed: list[tuple[float, float]] = []
    average_gain: float | None = None
    average_loss: float | None = None
    for index in range(1, len(levels)):
        previous, current = levels[index - 1], levels[index]
        if not _is_positive(previous) or not _is_positive(current):
            seed.clear()
            average_gain = average_loss = None
            continue
        assert previous is not None and current is not None
        change = current - previous
        gain, loss = max(change, 0.0), max(-change, 0.0)
        if average_gain is None or average_loss is None:
            seed.append((gain, loss))
            if len(seed) < period:
                continue
            average_gain = sum(pair[0] for pair in seed) / period
            average_loss = sum(pair[1] for pair in seed) / period
        else:
            average_gain = ((period - 1) * average_gain + gain) / period
            average_loss = ((period - 1) * average_loss + loss) / period
        if average_gain == 0.0 and average_loss == 0.0:
            output[index] = None
        elif average_loss == 0.0:
            output[index] = 100.0
        else:
            output[index] = 100.0 - 100.0 / (1.0 + average_gain / average_loss)
    return output


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    left_centered = [value - left_mean for value in left]
    right_centered = [value - right_mean for value in right]
    left_ss = sum(value * value for value in left_centered)
    right_ss = sum(value * value for value in right_centered)
    if left_ss == 0.0 or right_ss == 0.0:
        return None
    covariance = sum(a * b for a, b in zip(left_centered, right_centered, strict=True))
    return covariance / math.sqrt(left_ss * right_ss)


def _reference_non_null_source(levels: Sequence[float]) -> dict[str, list[float | None]]:
    """Calculate formulas on the compact source stream; invalid prices remain present."""
    size = len(levels)
    output: dict[str, list[float | None]] = {}
    rsi5 = _wilder_rsi(levels, 5)
    rsi15 = _wilder_rsi(levels, 15)  # internal intermediate; never a result key

    def ratio(index: int) -> float | None:
        numerator, denominator = rsi5[index], rsi15[index]
        if numerator is None or denominator is None or denominator == 0.0:
            return None
        return numerator / denominator

    def slope(index: int) -> float | None:
        if index < 3:
            return None
        current, prior = rsi5[index], rsi5[index - 3]
        if current is None or prior is None:
            return None
        return (current - prior) / 3.0

    def delta(index: int) -> float | None:
        if index < 1:
            return None
        current, prior = rsi5[index], rsi5[index - 1]
        if current is None or prior is None:
            return None
        return current - prior

    output["rsi_ratio_5_15"] = [ratio(index) for index in range(size)]
    output["rsi5_slope_3"] = [slope(index) for index in range(size)]
    output["rsi5_delta_1"] = [delta(index) for index in range(size)]

    rocs: dict[int, list[float | None]] = {}
    for period in (4, 8):
        rocs[period] = [
            None
            if index < period
            or not _is_positive(levels[index])
            or not _is_positive(levels[index - period])
            else levels[index] / levels[index - period] - 1.0
            for index in range(size)
        ]

    def roc_acceleration(index: int) -> float | None:
        roc4, roc8 = rocs[4][index], rocs[8][index]
        if roc4 is None or roc8 is None:
            return None
        return roc4 - roc8

    output["roc_acceleration_4_8"] = [roc_acceleration(index) for index in range(size)]

    valid_levels = [(index, value) for index, value in enumerate(levels) if _is_positive(value)]
    level_z: list[float | None] = [None] * size
    for position, (index, current) in enumerate(valid_levels):
        tail = [value for _, value in valid_levels[max(0, position - 95) : position + 1]]
        if len(tail) == 96:
            deviation = stdev(tail)
            if deviation != 0.0:
                level_z[index] = (current - sum(tail) / 96.0) / deviation
    output["level_zscore_96"] = level_z

    one_step: list[float | None] = [None] * size
    for index in range(1, size):
        previous, current = levels[index - 1], levels[index]
        if _is_positive(previous) and _is_positive(current):
            assert previous is not None and current is not None
            one_step[index] = math.log(current / previous)
    vol8: list[float | None] = [None] * size
    for index in range(size):
        log_window = one_step[max(0, index - 7) : index + 1]
        if len(log_window) == 8 and all(value is not None for value in log_window):
            vol8[index] = stdev([value for value in log_window if value is not None])
    volatility_z: list[float | None] = [None] * size
    valid_volatilities = [(index, value) for index, value in enumerate(vol8) if value is not None]
    for position, (index, current) in enumerate(valid_volatilities):
        tail = [value for _, value in valid_volatilities[max(0, position - 95) : position + 1]]
        if len(tail) == 96:
            deviation = stdev(tail)
            if deviation != 0.0:
                volatility_z[index] = (current - sum(tail) / 96.0) / deviation
    output["volatility_zscore_96"] = volatility_z

    for lag, pair_window in ((1, 96), (4, 96), (8, 192)):
        correlations: list[float | None] = [None] * size
        for index in range(size):
            pairs = [
                (one_step[right], one_step[right - lag])
                for right in range(lag, index + 1)
                if one_step[right] is not None and one_step[right - lag] is not None
            ]
            pairs = pairs[-pair_window:]
            if len(pairs) == pair_window:
                correlations[index] = _pearson(
                    [pair[0] for pair in pairs if pair[0] is not None],
                    [pair[1] for pair in pairs if pair[1] is not None],
                )
        output[f"momentum_autocorr_lag{lag}_window{pair_window}"] = correlations
    return output


def _reference(levels: Sequence[float | None]) -> dict[str, list[float | None]]:
    """Drop SQL-source NULL observations, compute, then restore timestamp alignment."""
    source_positions = [index for index, value in enumerate(levels) if value is not None]
    source_levels = [levels[index] for index in source_positions]
    assert all(value is not None for value in source_levels)
    calculated = _reference_non_null_source([value for value in source_levels if value is not None])
    expanded: dict[str, list[float | None]] = {}
    for name, observations in calculated.items():
        aligned: list[float | None] = [None] * len(levels)
        for index, value in zip(source_positions, observations, strict=True):
            aligned[index] = value
        expanded[name] = aligned
    return expanded


def _levels(count: int) -> list[float]:
    """Deterministic non-monotonic positive levels with hand-auditable changes."""
    return [100.0 + index * 0.08 + ((index * 17) % 11) * 0.37 for index in range(count)]


def test_pr171_feature_contract_is_closed_and_does_not_leak_to_raw_or_gold() -> None:
    expected = tuple(
        f"{series}_{suffix}"
        for series in PRICE_FEATURE_SERIES
        for suffix in _EXPECTED_NEW_FEATURE_SUFFIXES
    )
    current = set(FEATURE_COLUMNS)
    assert all(name in current for name in expected)
    assert len(FEATURE_COLUMNS) == len(set(FEATURE_COLUMNS))
    assert not any(name.endswith("_rsi_15obs") for name in FEATURE_COLUMNS)
    assert not set(expected).intersection(POSTGRES_RAW_COLUMNS)
    assert not set(expected).intersection(GOLD_COLUMNS)


def test_pr171_feature_family_order_follows_each_series_block_before_geometric_returns() -> None:
    names = FEATURE_COLUMNS
    for series in PRICE_FEATURE_SERIES:
        momentum_end = names.index(f"{series}_momentum_autocorr_20_120obs") + 1
        expected_block = tuple(f"{series}_{suffix}" for suffix in _EXPECTED_NEW_FEATURE_SUFFIXES)
        assert names[momentum_end : momentum_end + len(expected_block)] == expected_block
    last_series = PRICE_FEATURE_SERIES[-1]
    last_block_end = names.index(f"{last_series}_momentum_autocorr_lag8_window192")
    first_geometric = names.index(f"{PRICE_FEATURE_SERIES[0]}_return_geom_4obs_pct")
    assert last_block_end < first_geometric


def test_reference_rsi_composites_roc_acceleration_and_warmup() -> None:
    values = _reference(_levels(140))
    assert set(values) == set(_EXPECTED_NEW_FEATURE_SUFFIXES)
    assert values["rsi_ratio_5_15"][14] is None
    assert values["rsi_ratio_5_15"][15] is not None
    assert values["rsi5_slope_3"][7] is None
    assert values["rsi5_slope_3"][8] is not None
    assert values["rsi5_delta_1"][5] is None
    assert values["rsi5_delta_1"][6] is not None
    assert values["roc_acceleration_4_8"][7] is None
    assert values["roc_acceleration_4_8"][8] is not None
    acceleration = values["roc_acceleration_4_8"][-1]
    assert acceleration is not None
    assert math.isclose(
        acceleration,
        _levels(140)[-1] / _levels(140)[-5] - _levels(140)[-1] / _levels(140)[-9],
        rel_tol=1e-12,
    )


def test_reference_zscores_use_96_valid_values_sample_deviation_and_null_zero_variance() -> None:
    levels: list[float | None] = [*_levels(240)]
    levels[30] = None
    levels[77] = 0.0
    values = _reference(levels)
    assert values["level_zscore_96"][95] is None  # fewer than 96 valid values
    assert values["level_zscore_96"][97] is not None  # 96 valid positive values
    assert values["volatility_zscore_96"][100] is None
    assert any(value is not None for value in values["volatility_zscore_96"])
    level_samples = [value for value in levels if value is not None and value > 0.0][:96]
    expected_level_z = (level_samples[-1] - sum(level_samples) / 96.0) / stdev(level_samples)
    assert math.isclose(values["level_zscore_96"][97], expected_level_z)
    flat = _reference([100.0] * 240)
    assert all(value is None for value in flat["level_zscore_96"])
    assert all(value is None for value in flat["volatility_zscore_96"])
    declining = _reference([300.0 - index for index in range(120)])
    assert declining["rsi_ratio_5_15"][-1] is None  # RSI-15 denominator is zero.
    assert all(
        value is None or math.isfinite(value) for family in values.values() for value in family
    )


def test_reference_autocorrelation_counts_valid_pairs_and_keeps_negative_sign() -> None:
    # Log-return signs alternate, yielding a strong negative lag-1 correlation.
    alternating = [100.0]
    for index in range(1, 260):
        alternating.append(alternating[-1] * (1.012 if index % 2 else 1.0 / 1.012))
    values = _reference(alternating)
    assert values["momentum_autocorr_lag1_window96"][96] is None
    assert values["momentum_autocorr_lag1_window96"][97] is not None
    negative_correlation = values["momentum_autocorr_lag1_window96"][-1]
    assert negative_correlation is not None and negative_correlation < 0.0

    sparse: list[float | None] = list(_levels(500))
    for index in range(12, len(sparse), 19):
        sparse[index] = None
    sparse_values = _reference(sparse)
    assert sparse_values["momentum_autocorr_lag1_window96"][90] is None
    assert sparse_values["momentum_autocorr_lag1_window96"][-1] is not None

    constant = _reference([100.0] * 260)
    assert constant["momentum_autocorr_lag1_window96"][-1] is None
    assert constant["momentum_autocorr_lag4_window96"][-1] is None
    assert constant["momentum_autocorr_lag8_window192"][-1] is None


def test_reference_windows_are_causal_under_future_extension_and_truncation() -> None:
    prefix = _levels(260)
    extended = [*prefix, 10_000.0, 0.0, 0.1]
    before = _reference(prefix)
    after = _reference(extended)
    for feature, observations in before.items():
        assert observations == after[feature][: len(prefix)], feature
