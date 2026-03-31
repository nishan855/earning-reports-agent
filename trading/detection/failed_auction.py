"""
Setup 3: Failed Auction (V3.0 — Trigger & Grade)

3A: Value Area Rejection (VAR) — after 11:00 AM
3B: Major Level Rejection — 5m spotter / 1m sniper

Phase 1 Trigger: Location + candle shape only
Phase 2 Grade: Confidence scorer evaluates volume, CVD, approach
Phase 3 Gate: score >= 50 passes to agent
"""

from ..models import Candle, Level
from .approach import classify_approach
from .metrics import displacement_ratio, rolling_vol_ratio, cvd_turn_magnitude, wick_body_ratio
from .confidence import score_signal


def detect_failed_auction(
    candles: list,
    level: Level,
    atr: float,
    rolling_avg_vol: float,
    cvd_turn: float,
    rolling_avg_cvd: float,
    vah: float,
    val: float,
    poc: float,
    session_hour: float,
    cvd_quarantine: bool = False,
    day_bias: str = "NEUTRAL",
    candles_1m: list = None,
    rolling_avg_vol_1m: float = 0.0,
) -> dict | None:
    """Tries 3A first (5m chart), then 3B (5m spotter + 1m sniper)."""
    result = _detect_var(
        candles, level, atr, rolling_avg_vol, cvd_turn, rolling_avg_cvd,
        vah, val, poc, session_hour, cvd_quarantine, day_bias
    )
    if result:
        return result

    return _detect_major_level(
        candles, level, atr, rolling_avg_vol_1m or rolling_avg_vol,
        cvd_turn, rolling_avg_cvd,
        cvd_quarantine, day_bias,
        candles_1m=candles_1m,
    )


def _detect_var(
    candles, level, atr, rolling_avg_vol, cvd_turn, rolling_avg_cvd,
    vah, val, poc, session_hour, cvd_quarantine, day_bias
) -> dict | None:
    """Setup 3A: Value Area Rejection. After 11:00 AM.
    V3.0: Trigger on location + shape. Grade with scorer."""
    if session_hour < 11.0:
        return None
    if not vah or not val or not poc:
        return None
    if len(candles) < 3:
        return None

    candle = candles[-1]
    proximity = atr * 0.2

    # ── PHASE 1: TRIGGER — price interacts with VAH/VAL boundary ──
    # Classic: outside → closes back inside
    outside_above = candle.h > vah and candle.c < vah
    outside_below = candle.l < val and candle.c > val
    # Inside-out: approaches boundary from inside with rejection wick
    body = abs(candle.c - candle.o) or 0.001
    inside_touch_vah = (candle.h >= vah - proximity and candle.c < vah
                        and candle.c < candle.o
                        and (candle.h - max(candle.o, candle.c)) > body * 1.5)
    inside_touch_val = (candle.l <= val + proximity and candle.c > val
                        and candle.c > candle.o
                        and (min(candle.o, candle.c) - candle.l) > body * 1.5)

    if outside_above or inside_touch_vah:
        direction = "BEARISH"
    elif outside_below or inside_touch_val:
        direction = "BULLISH"
    else:
        return None

    # ── PHASE 2: GRADE — volume, CVD, approach scored ──
    vol_ratio = rolling_vol_ratio(candle, rolling_avg_vol)
    cvd_ratio = cvd_turn_magnitude(cvd_turn, rolling_avg_cvd)
    disp = displacement_ratio(candle)

    approach = classify_approach(
        candles[-6:-1] if len(candles) >= 6 else candles[:-1],
        level.price, atr, rolling_avg_vol,
    )

    conf = score_signal(
        level=level,
        vol_ratio=vol_ratio,
        displacement=disp,
        wick_ratio=wick_body_ratio(candle, direction),
        cvd_ratio=cvd_ratio,
        cvd_divergence=False,
        approach=approach,
        cvd_quarantine=cvd_quarantine,
        day_bias=day_bias,
    )

    # ── PHASE 3: FINAL GATE ──
    if conf.score < 50:
        return None

    return {
        "setup":      "FAILED_AUCTION_VAR",
        "direction":  direction,
        "entry":      None,
        "target":     poc,
        "approach":   approach,
        "confidence": conf,
        "vol_ratio":  vol_ratio,
        "cvd_ratio":  cvd_ratio,
        "details":    (
            f"{'outside VAH' if outside_above else 'outside VAL' if outside_below else 'inside touch'} "
            f"target=POC ${poc:.2f} vol={vol_ratio:.1f}x cvd={cvd_ratio:.1f}x conf={conf.score}"
        ),
    }


def _detect_major_level(
    candles_5m, level, atr, rolling_avg_vol_1m, cvd_turn, rolling_avg_cvd,
    cvd_quarantine, day_bias, candles_1m=None,
) -> dict | None:
    """Setup 3B: Major Level Rejection — 5m Spotter / 1m Sniper.
    V3.0: Trigger on proximity + wick shape. Grade with scorer.
    Level score >= 8 required."""
    if level.score < 8:
        return None
    if len(candles_5m) < 3:
        return None

    # ── Determine trigger candles ──
    trigger_candles = candles_1m if candles_1m and len(candles_1m) >= 3 else candles_5m
    candle = trigger_candles[-1]

    # ── PHASE 1: TRIGGER — proximity + wick shape ──
    proximity = atr * 0.2
    near_as_resistance = candle.h >= level.price - proximity
    near_as_support = candle.l <= level.price + proximity
    if not near_as_resistance and not near_as_support:
        return None

    upper_wick = candle.h - max(candle.o, candle.c)
    lower_wick = min(candle.o, candle.c) - candle.l
    body = abs(candle.c - candle.o) or 0.001

    upper_ratio = upper_wick / body
    lower_ratio = lower_wick / body

    # Wick/body >= 2.0 on rejection side (strict shape requirement stays)
    if near_as_resistance and candle.c < level.price and upper_ratio >= 2.0:
        direction = "BEARISH"
        wick_r = upper_ratio
    elif near_as_support and candle.c > level.price and lower_ratio >= 2.0:
        direction = "BULLISH"
        wick_r = lower_ratio
    else:
        return None

    # ── PHASE 2: GRADE — volume, CVD, approach all scored ──
    vol_ratio = rolling_vol_ratio(candle, rolling_avg_vol_1m)
    cvd_ratio = cvd_turn_magnitude(cvd_turn, rolling_avg_cvd)
    disp = displacement_ratio(candle)

    # 5m approach context (the "spotter")
    approach = classify_approach(
        candles_5m[-6:-1] if len(candles_5m) >= 6 else candles_5m[:-1],
        level.price, atr, rolling_avg_vol_1m,
    )

    conf = score_signal(
        level=level,
        vol_ratio=vol_ratio,
        displacement=disp,
        wick_ratio=wick_r,
        cvd_ratio=cvd_ratio,
        cvd_divergence=False,
        approach=approach,
        cvd_quarantine=cvd_quarantine,
        day_bias=day_bias,
    )

    # ── PHASE 3: FINAL GATE ──
    if conf.score < 50:
        return None

    return {
        "setup":      "FAILED_AUCTION_MAJOR",
        "direction":  direction,
        "entry":      None,
        "target":     None,
        "approach":   approach,
        "confidence": conf,
        "vol_ratio":  vol_ratio,
        "cvd_ratio":  cvd_ratio,
        "wick_ratio": wick_r,
        "details":    (
            f"wick/body={wick_r:.1f} vol={vol_ratio:.1f}x cvd={cvd_ratio:.1f}x "
            f"approach={approach.type} conf={conf.score}"
        ),
    }
