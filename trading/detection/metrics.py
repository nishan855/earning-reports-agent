"""
Shared detection metrics.
All calculations use rolling windows — no static thresholds.
"""

from ..models import Candle


def displacement_ratio(candle: Candle) -> float:
    """Body / total range. 1.0 = full body candle. 0.0 = doji."""
    total_range = candle.h - candle.l
    if total_range == 0:
        return 0.0
    body = abs(candle.c - candle.o)
    return body / total_range


def wick_body_ratio(candle: Candle, direction: str) -> float:
    """
    Wick-to-body ratio on the rejection side.
    direction = 'BULLISH' → upper wick rejected bears
    direction = 'BEARISH' → lower wick rejected bulls
    """
    body = abs(candle.c - candle.o)
    if body == 0:
        return 99.0  # doji — treat as max wick
    if direction == "BULLISH":
        wick = candle.h - max(candle.o, candle.c)
    else:
        wick = min(candle.o, candle.c) - candle.l
    return wick / body


def rolling_vol_ratio(candle: Candle, rolling_avg: float) -> float:
    """candle.v / rolling_avg(last_10_bars)"""
    if rolling_avg <= 0:
        return 1.0
    return candle.v / rolling_avg


def cvd_turn_magnitude(cvd_turn: float, rolling_avg_cvd: float) -> float:
    """abs(cvd_turn) / rolling_avg(last_10_cvd_turns)"""
    if rolling_avg_cvd <= 0:
        return 0.0
    return abs(cvd_turn) / rolling_avg_cvd


def detect_fvg(candles: list) -> tuple:
    """
    Fair Value Gap detection.
    FVG = gap between candle[-3].high and candle[-1].low (for bullish)
          gap between candle[-3].low  and candle[-1].high (for bearish)

    Returns (fvg_found: bool, fvg_midpoint: float, fvg_direction: str)
    """
    if len(candles) < 3:
        return False, 0.0, ""

    c1 = candles[-3]  # 3 candles back
    c3 = candles[-1]  # most recent

    # Bullish FVG: gap above c1.high below c3.low
    if c3.l > c1.h:
        mid = (c1.h + c3.l) / 2
        return True, mid, "BULLISH"

    # Bearish FVG: gap below c1.low above c3.high
    if c3.h < c1.l:
        mid = (c1.l + c3.h) / 2
        return True, mid, "BEARISH"

    return False, 0.0, ""


def is_super_candle(
    candle: Candle,
    atr: float,
    rolling_avg_vol: float,
    cvd_turn: float,
    rolling_avg_cvd: float,
    vol_percentile_90: float,
) -> bool:
    """
    Super-candle check — skip Gate 7 confirmation if True.
    All three conditions must be met:
    - Volume in top 10% of rolling window
    - Body > ATR
    - CVD turn > 4× rolling average
    """
    vol_top10  = candle.v >= vol_percentile_90
    body_large = abs(candle.c - candle.o) > atr
    cvd_strong = cvd_turn_magnitude(cvd_turn, rolling_avg_cvd) >= 4.0
    return vol_top10 and body_large and cvd_strong
