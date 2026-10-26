"""Independent, hand-calculable EMA recurrence used by PR-172 QA."""

from __future__ import annotations

import math
from collections.abc import Sequence

import pytest

EMA_OUTPUT_SUFFIXES = (
    "ema5_distance",
    "ema9_distance",
    "ema21_distance",
    "ema5_ema21_spread",
    "ema9_slope_3",
    "ema5_ema9_cross",
)


def independent_ema(levels: Sequence[float | None], period: int) -> list[float | None]:
    """Arithmetic seed, recursive EMA, and hard reset on every invalid source row."""
    alpha = 2.0 / (period + 1.0)
    seed: list[float] = []
    current: float | None = None
    result: list[float | None] = []
    for level in levels:
        if level is None or not math.isfinite(level) or level <= 0.0:
            seed.clear()
            current = None
            result.append(None)
            continue
        if current is None:
            seed.append(level)
            if len(seed) == period:
                current = sum(seed) / period
                seed.clear()
        else:
            current = alpha * level + (1.0 - alpha) * current
        result.append(current)
    return result


def independent_ema_features(
    levels: Sequence[float | None],
) -> dict[str, list[float | None]]:
    """Compute public EMA outputs without production SQL/catalog helpers."""
    ema5 = independent_ema(levels, 5)
    ema9 = independent_ema(levels, 9)
    ema21 = independent_ema(levels, 21)
    size = len(levels)
    features: dict[str, list[float | None]] = {
        suffix: [None] * size for suffix in EMA_OUTPUT_SUFFIXES
    }

    for index, level in enumerate(levels):
        if level is not None and math.isfinite(level) and level > 0.0:
            if ema5[index] is not None:
                features["ema5_distance"][index] = level / ema5[index] - 1.0
            if ema9[index] is not None:
                features["ema9_distance"][index] = level / ema9[index] - 1.0
            if ema21[index] is not None:
                features["ema21_distance"][index] = level / ema21[index] - 1.0
                if ema5[index] is not None:
                    features["ema5_ema21_spread"][index] = (ema5[index] - ema21[index]) / level
            if index >= 3 and ema9[index] is not None and ema9[index - 3] is not None:
                features["ema9_slope_3"][index] = (ema9[index] - ema9[index - 3]) / (3.0 * level)

        if index == 0:
            continue
        current_pair = (ema5[index], ema9[index])
        prior_pair = (ema5[index - 1], ema9[index - 1])
        if any(value is None for value in (*current_pair, *prior_pair)):
            continue
        fast, slow = current_pair
        prior_fast, prior_slow = prior_pair
        assert fast is not None and slow is not None
        assert prior_fast is not None and prior_slow is not None
        if fast > slow and prior_fast <= prior_slow:
            features["ema5_ema9_cross"][index] = 1.0
        elif fast < slow and prior_fast >= prior_slow:
            features["ema5_ema9_cross"][index] = -1.0
        else:
            features["ema5_ema9_cross"][index] = 0.0
    return features


@pytest.mark.unit
def test_independent_ema_reference_uses_arithmetic_seed_then_recursive_alpha() -> None:
    # EMA-5 seed is mean(2,4,6,8,10)=6; alpha=1/3 gives EMA(12)=8.
    assert independent_ema([2.0, 4.0, 6.0, 8.0, 10.0, 12.0], 5) == [
        None,
        None,
        None,
        None,
        6.0,
        8.0,
    ]


@pytest.mark.unit
@pytest.mark.parametrize("invalid", [None, 0.0, -3.0, math.inf, math.nan])
def test_independent_ema_reference_resets_seed_on_missing_or_nonpositive_level(
    invalid: float | None,
) -> None:
    before_break = [2.0, 4.0, 6.0, 8.0, 10.0, 12.0]
    after_break = [20.0, 22.0, 24.0, 26.0, 28.0]
    levels = [*before_break, invalid, *after_break]

    actual = independent_ema(levels, 5)

    assert actual[:5] == [None] * 4 + [6.0]
    assert actual[5] == 8.0
    assert actual[6] is None
    assert actual[7:11] == [None] * 4
    assert actual[11] == 24.0


@pytest.mark.unit
def test_independent_ema_outputs_pin_warmup_normalization_slope_and_cross_states() -> None:
    levels = [10.0] * 9 + [20.0] * 4 + [10.0] * 12
    actual = independent_ema_features(levels)

    # First complete EMA-9 seed, before EMA-21 has its 21-level seed.
    assert actual["ema5_distance"][8] == pytest.approx(0.0)
    assert actual["ema9_distance"][8] == pytest.approx(0.0)
    assert actual["ema21_distance"][8] is None
    assert actual["ema5_ema21_spread"][20] == pytest.approx(-0.15916509479482582)
    assert actual["ema9_slope_3"][9] is None
    # EMA9 is 12 at row 9 and 15.904 at row 12: (15.904-12)/(3*20).
    assert actual["ema9_slope_3"][12] == pytest.approx(3.904 / 60.0)
    assert actual["ema5_ema9_cross"][8] is None
    assert actual["ema5_ema9_cross"][9] == pytest.approx(1.0)
    assert actual["ema5_ema9_cross"][10] == pytest.approx(0.0)
    assert actual["ema5_ema9_cross"][14] == pytest.approx(-1.0)
