"""
Setup 2: Institutional Order Block Defense
Replaces breakout.py

Detects institutional defense of order blocks on 5m candles.
Only valid on TREND days with locked directional bias.
"""

from ..models import Candle, Level
from ..context.sim_clock import now_et
from .approach import classify_approach
from .metrics import displacement_ratio, rolling_vol_ratio, cvd_turn_magnitude
from .confidence import score_signal


def find_order_block(
    candles_5m: list,   # last 20+ closed 5m candles
    displacement_candle_idx: int,  # index of the displacement candle
    direction: str,     # BULLISH = looking for last bearish OB before bull displacement
    atr: float,
    rolling_avg_vol: float,
) -> dict | None:
    """
    Order Block = last opposite-colored candle before displacement.
    Requirements:
      - body > ATR × 1.0
      - volume top 20% (vol_ratio >= 1.5)
      - formed within 60 min from OB candle close time
      - not visited 2+ times since formed
    """
    if displacement_candle_idx < 1:
        return None

    ob_color = "BEARISH" if direction == "BULLISH" else "BULLISH"
    now_ts   = int(now_et().timestamp() * 1000)  # ms — uses sim clock in sim mode

    # Search backwards from displacement candle
    for i in range(displacement_candle_idx - 1, max(-1, displacement_candle_idx - 15), -1):
        c = candles_5m[i]
        is_correct_color = (
            (ob_color == "BEARISH" and c.c < c.o) or
            (ob_color == "BULLISH" and c.c > c.o)
        )
        if not is_correct_color:
            continue

        body = abs(c.c - c.o)
        if body < atr * 1.0:
            continue

        vol_ratio = c.v / rolling_avg_vol if rolling_avg_vol > 0 else 0
        if vol_ratio < 1.5:
            continue

        # Formed within 60 min (3600000 ms)
        if now_ts - c.t > 3600000:
            continue

        # OB zone
        if direction == "BULLISH":
            ob_high = max(c.o, c.c)
            ob_low  = min(c.o, c.c)
        else:
            ob_high = max(c.o, c.c)
            ob_low  = min(c.o, c.c)

        return {
            "candle":    c,
            "ob_high":   ob_high,
            "ob_low":    ob_low,
            "ob_mid":    (ob_high + ob_low) / 2,
            "vol_ratio": vol_ratio,
            "formed_at": c.t,
        }

    return None


def count_ob_visits(
    candles_5m: list,
    ob_high: float,
    ob_low: float,
    ob_formed_idx: int,
) -> int:
    """Count how many times price touched OB zone after it was formed."""
    visits = 0
    for c in candles_5m[ob_formed_idx + 1:]:
        if c.l <= ob_high and c.h >= ob_low:
            visits += 1
    return visits


def detect_ob_defense(
    candles_5m: list,       # last 20+ closed 5m candles
    candles_1m: list,       # last 5+ closed 1m candles for CVD slope
    level: Level,
    atr: float,
    rolling_avg_vol_5m: float,
    rolling_avg_vol_1m: float,
    cvd_turn: float,
    rolling_avg_cvd: float,
    day_type: str,          # must be TREND
    day_bias: str,          # must be BULLISH or BEARISH (locked)
    cvd_quarantine: bool = False,
) -> dict | None:
    """
    Returns signal dict or None.
    Only fires on TREND days with locked directional bias.
    """
    # Day type gate
    if day_type != "TREND":
        return None

    if day_bias not in ("BULLISH", "BEARISH"):
        return None

    direction = day_bias  # signal direction matches locked bias

    if len(candles_5m) < 5:
        return None

    last_5m = candles_5m[-1]

    # Approach on 5m candles must be ABSORPTION
    approach = classify_approach(
        candles_5m[-6:-1], level.price, atr, rolling_avg_vol_5m
    )
    if approach.type != "ABSORPTION":
        return None

    # Find most recent displacement to locate OB
    displacement_idx = len(candles_5m) - 1
    ob = find_order_block(
        candles_5m, displacement_idx, direction, atr, rolling_avg_vol_5m
    )
    if not ob:
        return None

    # OB not visited 2+ times since formed
    ob_formed_idx = next(
        (i for i, c in enumerate(candles_5m) if c.t == ob["formed_at"]), 0
    )
    visits = count_ob_visits(candles_5m, ob["ob_high"], ob["ob_low"], ob_formed_idx)
    if visits >= 2:
        return None

    # Proximity interaction: price within ATR × 0.2 of OB zone (replaces pixel-perfect pierce)
    proximity = atr * 0.2
    if direction == "BULLISH":
        touched_ob = last_5m.l <= ob["ob_high"] + proximity
    else:
        touched_ob = last_5m.h >= ob["ob_low"] - proximity

    if not touched_ob:
        return None

    # CVD slope: declining before → flattens/turns at level
    cvd_ratio  = cvd_turn_magnitude(cvd_turn, rolling_avg_cvd)
    vol_ratio  = rolling_vol_ratio(last_5m, rolling_avg_vol_5m)
    disp       = displacement_ratio(last_5m)

    # CVD must show defense (turn positive for bull, negative for bear)
    if not cvd_quarantine:
        if direction == "BULLISH" and cvd_turn <= 0:
            return None
        if direction == "BEARISH" and cvd_turn >= 0:
            return None

    conf = score_signal(
        level=level,
        vol_ratio=vol_ratio,
        displacement=disp,
        wick_ratio=0.0,
        cvd_ratio=cvd_ratio,
        cvd_divergence=False,
        approach=approach,
        cvd_quarantine=cvd_quarantine,
        day_bias=day_bias,
    )

    if conf.label == "LOW":
        return None

    return {
        "setup":        "OB_DEFENSE",
        "direction":    direction,
        "entry":        None,  # next candle open
        "approach":     approach,
        "confidence":   conf,
        "ob":           ob,
        "ob_visits":    visits,
        "vol_ratio":    vol_ratio,
        "cvd_ratio":    cvd_ratio,
        "details":      (
            f"OB ${ob['ob_low']:.2f}–${ob['ob_high']:.2f} "
            f"visits={visits} vol={vol_ratio:.1f}x cvd={cvd_ratio:.1f}x"
        ),
    }
