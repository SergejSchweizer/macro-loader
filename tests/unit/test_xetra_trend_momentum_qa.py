"""Independent QA references for PR-133 trend and momentum features."""

from __future__ import annotations

import math

SMA_RATIOS = ((5, 20), (10, 20), (10, 40))
RSI_WINDOWS = (7, 14)
ROC_WINDOWS = (3, 5, 10, 20)
DRAWDOWN_WINDOWS = (20, 60)


def _window(values: list[float | None], index: int, size: int) -> list[float] | None:
    if index < size - 1:
        return None
    values_in_window = values[index - size + 1 : index + 1]
    if any(value is None for value in values_in_window):
        return None
    return [value for value in values_in_window if value is not None]


def _wilder_rsi(levels: list[float | None], period: int) -> list[float | None]:
    gains: list[float | None] = [None]
    losses: list[float | None] = [None]
    for current, previous in zip(levels[1:], levels[:-1], strict=True):
        if current is None or previous is None or current <= 0 or previous <= 0:
            gains.append(None)
            losses.append(None)
        else:
            gains.append(max(current - previous, 0.0))
            losses.append(max(previous - current, 0.0))
    result: list[float | None] = [None] * len(levels)
    if len(levels) <= period:
        return result
    seed_gains = gains[1 : period + 1]
    seed_losses = losses[1 : period + 1]
    if any(value is None for value in (*seed_gains, *seed_losses)):
        return result
    average_gain = sum(value for value in seed_gains if value is not None) / period
    average_loss = sum(value for value in seed_losses if value is not None) / period
    for index in range(period, len(levels)):
        if index > period:
            gain = gains[index]
            loss = losses[index]
            if gain is None or loss is None:
                average_gain = math.nan
                average_loss = math.nan
            else:
                average_gain = (average_gain * (period - 1) + gain) / period
                average_loss = (average_loss * (period - 1) + loss) / period
        if math.isnan(average_gain) or math.isnan(average_loss):
            result[index] = None
        elif average_loss == 0:
            result[index] = 100.0
        else:
            result[index] = 100.0 - 100.0 / (1.0 + average_gain / average_loss)
    return result


def independent_trend_features(levels: list[float | None]) -> dict[str, list[float | None]]:
    result: dict[str, list[float | None]] = {}
    for short, long in SMA_RATIOS:
        values: list[float | None] = []
        for index in range(len(levels)):
            short_values = _window(levels, index, short)
            long_values = _window(levels, index, long)
            values.append(
                None
                if short_values is None or long_values is None or sum(long_values) == 0
                else (sum(short_values) / short) / (sum(long_values) / long)
            )
        result[f"sma_ratio_{short}_{long}"] = values
    for period in RSI_WINDOWS:
        result[f"rsi_{period}"] = _wilder_rsi(levels, period)
    for period in ROC_WINDOWS:
        result[f"roc_{period}"] = [
            None
            if index < period
            or levels[index] is None
            or levels[index] <= 0
            or levels[index - period] is None
            or levels[index - period] <= 0
            else levels[index] / levels[index - period] - 1.0
            for index in range(len(levels))
        ]
    for period in DRAWDOWN_WINDOWS:
        values = []
        for index in range(len(levels)):
            observations = _window(levels, index, period)
            values.append(
                None
                if observations is None or any(value <= 0 for value in observations)
                else levels[index] / max(observations) - 1.0
            )
        result[f"drawdown_{period}"] = values
    return result


def test_reference_covers_all_pr133_columns_and_warmup() -> None:
    levels = [100.0 + index for index in range(65)]
    result = independent_trend_features(levels)

    assert set(result) == {
        *(f"sma_ratio_{short}_{long}" for short, long in SMA_RATIOS),
        *(f"rsi_{period}" for period in RSI_WINDOWS),
        *(f"roc_{period}" for period in ROC_WINDOWS),
        *(f"drawdown_{period}" for period in DRAWDOWN_WINDOWS),
    }
    assert result["sma_ratio_5_20"][18] is None
    assert result["sma_ratio_5_20"][19] is not None
    assert result["rsi_7"][6] is None
    assert result["rsi_7"][7] == 100.0
    assert result["roc_20"][19] is None
    assert result["roc_20"][20] is not None
    assert result["drawdown_60"][59] is not None


def test_reference_matches_hand_calculable_trend_values() -> None:
    levels = [100.0] * 20 + [110.0] * 5
    result = independent_trend_features(levels)

    assert result["sma_ratio_5_20"][-1] == (110.0 / 102.5)
    assert result["rsi_7"][-1] == 100.0
    assert math.isclose(result["roc_20"][-1], 0.1)
    assert result["drawdown_20"][-1] == 0.0


def test_reference_is_causal_and_fail_closed_for_invalid_levels() -> None:
    levels = [100.0 + index for index in range(65)]
    changed_future = [*levels[:-1], 10_000.0]
    original = independent_trend_features(levels)
    changed = independent_trend_features(changed_future)

    assert original["roc_5"][-2] == changed["roc_5"][-2]
    invalid = independent_trend_features([100.0, 0.0, 102.0] + levels)
    assert invalid["roc_3"][4] is None
    assert invalid["rsi_7"][8] is None
    assert invalid["drawdown_20"][20] is None
