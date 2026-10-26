"""Independent mathematical oracle for the GOALS relative-MACD contract."""

from __future__ import annotations

import math
from collections.abc import Sequence

import pytest

_QA_PRICE_SERIES = (
    "vix",
    "vix9d",
    "vix3m",
    "vix6m",
    "vix1y",
    "vstoxx",
    "move",
    "ciss",
    "euro_hy_oas",
    "us_2y",
    "us_10y",
    "usd_broad",
)
_MACD_OUTPUTS = (
    "macd_line_relative",
    "macd_hist_relative",
    "macd_hist_delta_3",
)


def _ema(values: Sequence[float | None], period: int, *, positive_only: bool) -> list[float | None]:
    """Arithmetic seed followed by the XETRA recurrence; NULL/nonfinite rows reset it."""
    alpha = 2.0 / (period + 1.0)
    seed: list[float] = []
    current: float | None = None
    result: list[float | None] = []
    for value in values:
        valid = value is not None and math.isfinite(value)
        if valid and positive_only:
            valid = value > 0.0
        if not valid:
            seed.clear()
            current = None
            result.append(None)
            continue
        assert value is not None
        if current is None:
            seed.append(value)
            if len(seed) == period:
                current = sum(seed) / period
                seed.clear()
        else:
            current = alpha * value + (1.0 - alpha) * current
        result.append(current)
    return result


def _macd_reference(levels: Sequence[float | None]) -> dict[str, list[float | None]]:
    """Compute the three public outputs without importing production code."""
    fast = _ema(levels, 12, positive_only=True)
    slow = _ema(levels, 26, positive_only=True)
    line = [
        fast_value - slow_value if fast_value is not None and slow_value is not None else None
        for fast_value, slow_value in zip(fast, slow, strict=True)
    ]
    signal = _ema(line, 9, positive_only=False)
    line_relative: list[float | None] = []
    histogram: list[float | None] = []
    histogram_delta: list[float | None] = []
    for index, level in enumerate(levels):
        valid_level = level is not None and math.isfinite(level) and level > 0.0
        line_relative.append(
            line[index] / level if valid_level and line[index] is not None else None
        )
        histogram.append(
            (line[index] - signal[index]) / level
            if valid_level and line[index] is not None and signal[index] is not None
            else None
        )
        prior = histogram[index - 3] if index >= 3 else None
        current = histogram[-1]
        histogram_delta.append(
            current - prior if current is not None and prior is not None else None
        )
    return {
        "macd_line_relative": line_relative,
        "macd_hist_relative": histogram,
        "macd_hist_delta_3": histogram_delta,
    }


def test_ema_uses_arithmetic_seed_then_alpha_two_over_period_plus_one() -> None:
    result = _ema((1.0, 2.0, 3.0, 6.0), 3, positive_only=True)

    assert result[:3] == [None, None, 2.0]
    assert result[3] == pytest.approx(4.0)


def test_level_ema_resets_on_null_nonfinite_and_nonpositive_observations() -> None:
    result = _ema((10.0, 11.0, 12.0, None, 20.0, 0.0, 30.0, 31.0, 32.0), 3, positive_only=True)

    assert result == [None, None, 11.0, None, None, None, None, None, 31.0]


def test_signal_ema_seeds_and_recurs_on_signed_and_zero_macd_values() -> None:
    result = _ema((None, -2.0, -1.0, 0.0, 1.0, None, 5.0, 0.0, -5.0), 3, positive_only=False)

    assert result == [None, None, None, -1.0, 0.0, None, None, None, 0.0]


def test_macd_line_signal_and_histogram_warm_up_on_observations_26_and_34() -> None:
    levels = [100.0 + index for index in range(40)]
    result = _macd_reference(levels)

    for output in _MACD_OUTPUTS:
        assert all(value is None for value in result[output][:25])
    assert result["macd_line_relative"][25] is not None
    assert all(value is None for value in result["macd_hist_relative"][:33])
    assert result["macd_hist_relative"][33] is not None
    assert result["macd_hist_delta_3"][36] is not None


def test_macd_signal_handles_negative_and_zero_line_states() -> None:
    levels = [100.0] * 26 + [100.0] * 10 + [80.0 - index for index in range(20)]
    result = _macd_reference(levels)

    # Flat positive levels produce an exactly-zero line/signal; a sharp decline
    # subsequently creates a negative line while the signal remains finite.
    assert result["macd_line_relative"][25] == 0.0
    assert result["macd_hist_relative"][33] == 0.0
    assert any(value is not None and value < 0.0 for value in result["macd_line_relative"][36:])
    assert any(value is not None for value in result["macd_hist_relative"][36:])


def test_null_and_nonpositive_levels_reset_all_macd_stages_and_reseed() -> None:
    levels: list[float | None] = [100.0 + i for i in range(38)]
    levels.extend((None, *[90.0 + i for i in range(34)]))
    levels.extend((0.0, *[120.0 + i for i in range(34)]))
    result = _macd_reference(levels)

    null_index = 38
    zero_index = 73
    assert all(result[output][null_index] is None for output in _MACD_OUTPUTS)
    assert all(result[output][zero_index] is None for output in _MACD_OUTPUTS)
    assert result["macd_line_relative"][null_index + 26] is not None
    assert result["macd_hist_relative"][null_index + 34] is not None
    assert result["macd_hist_relative"][zero_index + 34] is not None


def test_histogram_delta_uses_exactly_three_source_rows_and_propagates_nulls() -> None:
    levels = [100.0 + index for index in range(42)]
    result = _macd_reference(levels)

    hist = result["macd_hist_relative"]
    delta = result["macd_hist_delta_3"]
    assert hist[38] is not None and hist[35] is not None
    assert delta[38] == pytest.approx(hist[38] - hist[35])
    assert delta[2] is None  # warm-up and unavailable t-3 endpoint propagate NULL

    levels[36] = None
    reset = _macd_reference(levels)
    assert reset["macd_hist_relative"][36] is None
    assert reset["macd_hist_delta_3"][36] is None


def test_reference_is_causal_under_future_truncation() -> None:
    levels = [100.0 + math.sin(index / 3.0) * 7.0 + index * 0.2 for index in range(80)]
    full = _macd_reference(levels)
    cutoff = 57
    truncated = _macd_reference(levels[:cutoff])

    for output in _MACD_OUTPUTS:
        for actual, expected in zip(truncated[output], full[output][:cutoff], strict=True):
            if expected is None:
                assert actual is None
            else:
                assert actual == pytest.approx(expected)


def test_expected_series_output_contract_is_explicit_and_excludes_estr() -> None:
    expected = tuple(
        f"{series}_{output}" for series in _QA_PRICE_SERIES for output in _MACD_OUTPUTS
    )

    assert len(_QA_PRICE_SERIES) == 12
    assert len(expected) == 36
    assert len(set(expected)) == 36
    assert all(not column.startswith("estr_") for column in expected)
