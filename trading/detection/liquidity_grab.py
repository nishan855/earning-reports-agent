"""
Setup 1: Liquidity Grab (V3.0 — Trigger & Grade)

Phase 1 Trigger: Physical sweep through level (S1 keeps strict sweep requirement)
Phase 2 Grade: Confidence scorer evaluates volume, CVD, approach, displacement
Phase 3 Gate: score >= 50 passes to agent
"""

from ..models import Candle, Level
from .approach import classify_approach, ApproachResult
from .metrics import (
    displacement_ratio, rolling_vol_ratio,
    cvd_turn_magnitude, detect_fvg,
)
from .confidence import score_signal


def detect_liquidity_grab(
    candles_1m: list,
    level: Level,
    atr: float,
    rolling_avg_vol: float,
    cvd_turn: float,
    rolling_avg_cvd: float,
    cvd_quarantine: bool = False,
    day_bias: str = "NEUTRAL",
) -> dict | None:
    if len(candles_1m) < 5:
        return None

    candle = candles_1m[-1]

    # ── PHASE 1: TRIGGER — physical sweep + close back (S1 purity) ──
    swept_below = candle.l < level.price and candle.c > level.price
    swept_above = candle.h > level.price and candle.c < level.price

    if not swept_below and not swept_above:
        return None

    direction = "BULLISH" if swept_below else "BEARISH"

    # Minimum wick past level (noise filter — not a gate, just sanity)
    if swept_below:
        wick_past = level.price - candle.l
    else:
        wick_past = candle.h - level.price

    if wick_past < atr * 0.25:
        return None  # filter bid/ask flutter — require meaningful penetration

    # ── PHASE 2: GRADE — everything else scored, not gated ──
    body = abs(candle.c - candle.o) or 0.001
    disp = displacement_ratio(candle)
    vol_ratio = rolling_vol_ratio(candle, rolling_avg_vol)
    cvd_ratio = cvd_turn_magnitude(cvd_turn, rolling_avg_cvd)

    approach = classify_approach(
        candles_1m[-6:-1] if len(candles_1m) >= 6 else candles_1m[:-1],
        level.price, atr, rolling_avg_vol,
    )

    fvg_found, fvg_mid, fvg_dir = detect_fvg(candles_1m[-3:])

    conf = score_signal(
        level=level,
        vol_ratio=vol_ratio,
        displacement=disp,
        wick_ratio=wick_past / body,
        cvd_ratio=cvd_ratio,
        cvd_divergence=False,
        approach=approach,
        cvd_quarantine=cvd_quarantine,
        day_bias=day_bias,
    )

    # ── PHASE 3: FINAL GATE — score >= 50 ──
    if conf.score < 50:
        return None

    return {
        "setup":        "LIQUIDITY_GRAB",
        "direction":    direction,
        "entry":        fvg_mid if fvg_found else None,
        "approach":     approach,
        "confidence":   conf,
        "fvg_found":    fvg_found,
        "fvg_midpoint": fvg_mid,
        "vol_ratio":    vol_ratio,
        "cvd_ratio":    cvd_ratio,
        "wick_past":    wick_past,
        "details":      (
            f"sweep wick={wick_past:.2f} disp={disp:.2f} "
            f"vol={vol_ratio:.1f}x cvd={cvd_ratio:.1f}x conf={conf.score} "
            f"fvg={'YES @ '+str(round(fvg_mid,2)) if fvg_found else 'NO'}"
        ),
    }
