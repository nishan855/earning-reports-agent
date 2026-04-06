"""
Setup 1: Liquidity Grab (V4.0 — 5m Sweep + 1m Momentum)

Detection on 5m bar close:
  Phase 1 Trigger: 5m bar sweeps through level, closes back, volume ≥ 1.2×
  Phase 2 Grade: Confidence scorer (inverted wick scoring) + 1m enrichment
  Phase 3 Gate: score >= threshold

Confirmation on 1m bar close (max 3 bars):
  Sharp 1m momentum candle in reversal direction
  Invalidation: price breaks wick extreme
"""

from ..models import Candle, Level
from .approach import classify_approach, ApproachResult
from .metrics import (
    displacement_ratio, rolling_vol_ratio,
    cvd_turn_magnitude, detect_fvg, get_5m_trend,
)
from .confidence import score_signal


def detect_5m_sweep(
    bars_5m: list,
    level: Level,
    atr: float,
    rolling_avg_vol: float,
    cvd_turn: float,
    rolling_avg_cvd: float,
    cvd_quarantine: bool = False,
    day_bias: str = "NEUTRAL",
    bars_1m_inside: list = None,
    atr_1m: float = 0.0,
    rolling_avg_vol_1m: float = 0.0,
) -> dict | None:
    """Detect institutional liquidity sweep on 5m bar.
    Returns sweep data dict or None."""
    if len(bars_5m) < 5:
        return None

    bar = bars_5m[-1]

    # Doji filter — bar must have meaningful body (not noise)
    bar_body = abs(bar.c - bar.o)
    bar_range = bar.h - bar.l
    if bar_range > 0 and bar_body / bar_range < 0.12:
        return None  # doji — both wicks dominate, no directional conviction

    # ── PHASE 1: TRIGGER — 5m sweep + close back ──
    swept_below = bar.l < level.price and bar.c > level.price
    swept_above = bar.h > level.price and bar.c < level.price

    if not swept_below and not swept_above:
        return None

    direction = "BULLISH" if swept_below else "BEARISH"

    # Wick penetration (5m threshold = ATR × 0.3, larger than 1m)
    if swept_below:
        wick_past = level.price - bar.l
        wick_extreme = bar.l
    else:
        wick_past = bar.h - level.price
        wick_extreme = bar.h

    if wick_past < atr * 0.3:
        return None

    # Volume HARD GATE — stops trigger = volume spike
    vol_ratio = rolling_vol_ratio(bar, rolling_avg_vol)
    if vol_ratio < 1.2:
        return None

    # ── PHASE 2: GRADE ──
    bar_range = bar.h - bar.l
    wick_rejection = wick_past / bar_range if bar_range > 0 else 0.0
    cvd_ratio = cvd_turn_magnitude(cvd_turn, rolling_avg_cvd)

    approach = classify_approach(
        bars_5m[-6:-1] if len(bars_5m) >= 6 else bars_5m[:-1],
        level.price, atr, rolling_avg_vol,
    )

    trend_5m = get_5m_trend(bars_5m) if len(bars_5m) >= 4 else "NEUTRAL"

    # FVG in the displacement (last 3 bars of 5m)
    fvg_found, fvg_mid, fvg_dir = detect_fvg(bars_5m[-3:])
    if fvg_found and fvg_dir != direction:
        fvg_found, fvg_mid = False, 0.0  # wrong direction FVG

    # 1m enrichment scoring
    enrichment = {"absorption": 0, "vol_cluster": 0, "cvd_micro": 0, "total": 0}
    if bars_1m_inside and len(bars_1m_inside) >= 2:
        enrichment = score_1m_enrichment(
            bars_1m_inside, direction, level.price,
            atr_1m if atr_1m > 0 else atr / 2.2,
            rolling_avg_vol_1m if rolling_avg_vol_1m > 0 else rolling_avg_vol / 5,
        )

    conf = score_signal(
        level=level,
        vol_ratio=vol_ratio,
        displacement=wick_rejection,  # INVERTED: high wick = high score
        wick_ratio=wick_rejection,
        cvd_ratio=cvd_ratio,
        cvd_divergence=False,
        approach=approach,
        cvd_quarantine=cvd_quarantine,
        day_bias=day_bias,
        trend_5m=trend_5m,
        signal_dir=direction,
        setup_type="LIQUIDITY_GRAB_5M",
        tests_today=level.tests_today,
    )

    # Add 1m enrichment bonus (capped at 100)
    enrichment_bonus = enrichment.get("total", 0)
    conf.score = min(100, conf.score + enrichment_bonus)
    if conf.score >= 75:
        conf.label = "HIGH"
    elif conf.score >= conf.threshold:
        conf.label = "MEDIUM"

    # ── PHASE 3: GATE ──
    if conf.score < conf.threshold:
        return None

    return {
        "setup":            "LIQUIDITY_GRAB",
        "direction":        direction,
        "entry":            fvg_mid if fvg_found else None,
        "approach":         approach,
        "confidence":       conf,
        "fvg_found":        fvg_found,
        "fvg_midpoint":     fvg_mid,
        "vol_ratio":        vol_ratio,
        "cvd_ratio":        cvd_ratio,
        "wick_past":        wick_past,
        "wick_extreme":     wick_extreme,
        "wick_rejection":   wick_rejection,
        "trend_5m":         trend_5m,
        "trend_pts":        conf.components.get("trend_5m", 0),
        "enrichment":       enrichment,
        "details": (
            f"5m sweep wick={wick_past:.2f} rejection={wick_rejection:.0%} "
            f"vol={vol_ratio:.1f}x cvd={cvd_ratio:.1f}x trend={trend_5m} "
            f"enrich=+{enrichment_bonus} conf={conf.score} "
            f"fvg={'YES @ '+str(round(fvg_mid,2)) if fvg_found else 'NO'}"
        ),
    }


def score_1m_enrichment(
    bars_1m_inside: list,
    direction: str,
    level_price: float,
    atr_1m: float,
    rolling_avg_vol_1m: float,
) -> dict:
    """Score the microstructure quality of a 5m sweep using its 1m bars."""
    if not bars_1m_inside or len(bars_1m_inside) < 2:
        return {"absorption": 0, "vol_cluster": 0, "cvd_micro": 0, "total": 0}

    # ── Absorption speed: how fast did price snap back after hitting extreme? ──
    if direction == "BULLISH":
        extreme_idx = min(range(len(bars_1m_inside)), key=lambda i: bars_1m_inside[i].l)
        bars_after = len(bars_1m_inside) - 1 - extreme_idx
        snapped_back = any(b.c > level_price for b in bars_1m_inside[extreme_idx + 1:])
    else:
        extreme_idx = max(range(len(bars_1m_inside)), key=lambda i: bars_1m_inside[i].h)
        bars_after = len(bars_1m_inside) - 1 - extreme_idx
        snapped_back = any(b.c < level_price for b in bars_1m_inside[extreme_idx + 1:])

    if snapped_back and bars_after >= 3:
        absorption = 8  # fast: extreme hit early, multiple bars recovered
    elif snapped_back and bars_after >= 1:
        absorption = min(8, bars_after * 3)  # 1 bar=3, 2 bars=6, 3+=8
    else:
        absorption = 0  # never snapped back within the 5m bar

    # ── Volume clustering: was highest volume at the extreme? ──
    volumes = [b.v for b in bars_1m_inside]
    if volumes:
        max_vol_idx = volumes.index(max(volumes))
        # Extreme bar or adjacent bar had highest volume = stop triggers
        vol_cluster = 5 if abs(max_vol_idx - extreme_idx) <= 1 else 0
    else:
        vol_cluster = 0

    # ── CVD micro-turn: did order flow reverse during the sweep bar? ──
    if len(bars_1m_inside) >= 3:
        first_half = bars_1m_inside[:len(bars_1m_inside) // 2]
        second_half = bars_1m_inside[len(bars_1m_inside) // 2:]
        first_bias = sum(1 if b.c > b.o else -1 for b in first_half)
        second_bias = sum(1 if b.c > b.o else -1 for b in second_half)

        if direction == "BULLISH":
            # First half bearish (driving into level), second half bullish (snap back)
            cvd_micro = 5 if first_bias < 0 and second_bias > 0 else 0
        else:
            cvd_micro = 5 if first_bias > 0 and second_bias < 0 else 0
    else:
        cvd_micro = 0

    total = absorption + vol_cluster + cvd_micro
    return {"absorption": absorption, "vol_cluster": vol_cluster, "cvd_micro": cvd_micro, "total": total}


# Keep old function for S3B and other callers that still reference it
def detect_liquidity_grab(*args, **kwargs):
    """Deprecated — V4.0 uses detect_5m_sweep() from on_5m_close."""
    return None
