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
    vah, val, poc, session_hour, cvd_quarantine, day_bias,
    bars_1m_inside=None, atr_1m=0.0, rolling_avg_vol_1m=0.0,
) -> dict | None:
    """Setup 3A: Value Area Rejection — V4.0 5m detection.
    After 11:00 AM. Price pushed outside value area and failed.
    Target is always POC. Low volume outside = confirmation."""
    if session_hour < 11.0:
        return None
    if not vah or not val or not poc:
        return None
    if len(candles) < 3:
        return None

    candle = candles[-1]

    # Doji filter — bar must have directional body
    candle_body = abs(candle.c - candle.o)
    candle_range = candle.h - candle.l
    if candle_range > 0 and candle_body / candle_range < 0.12:
        return None  # doji — no directional conviction for VAR

    proximity = atr * 0.2

    # ── PHASE 1: TRIGGER — 5m bar interacts with VAH/VAL boundary ──
    outside_above = candle.h > vah and candle.c < vah
    outside_below = candle.l < val and candle.c > val
    body = abs(candle.c - candle.o) or 0.001
    inside_touch_vah = (candle.h >= vah - proximity and candle.c < vah
                        and candle.c < candle.o
                        and (candle.h - max(candle.o, candle.c)) > body * 1.5)
    inside_touch_val = (candle.l <= val + proximity and candle.c > val
                        and candle.c > candle.o
                        and (min(candle.o, candle.c) - candle.l) > body * 1.5)

    if outside_above or inside_touch_vah:
        direction = "BEARISH"
        wick_extreme = candle.h
        var_type = "outside VAH" if outside_above else "inside touch VAH"
    elif outside_below or inside_touch_val:
        direction = "BULLISH"
        wick_extreme = candle.l
        var_type = "outside VAL" if outside_below else "inside touch VAL"
    else:
        return None

    # Wick rejection measurement
    bar_range = candle.h - candle.l
    if direction == "BEARISH":
        wick_past = candle.h - max(candle.o, candle.c)
    else:
        wick_past = min(candle.o, candle.c) - candle.l
    wick_rejection = wick_past / bar_range if bar_range > 0 else 0

    # ── PHASE 2: GRADE ──
    vol_ratio = rolling_vol_ratio(candle, rolling_avg_vol)
    cvd_ratio = cvd_turn_magnitude(cvd_turn, rolling_avg_cvd)

    approach = classify_approach(
        candles[-6:-1] if len(candles) >= 6 else candles[:-1],
        level.price, atr, rolling_avg_vol,
    )

    from .metrics import get_5m_trend
    trend_5m = get_5m_trend(candles) if len(candles) >= 4 else "NEUTRAL"

    # 1m enrichment
    enrichment = {"absorption": 0, "vol_cluster": 0, "cvd_micro": 0, "total": 0}
    if bars_1m_inside and len(bars_1m_inside) >= 2:
        from ..detection.liquidity_grab import score_1m_enrichment
        enrichment = score_1m_enrichment(
            bars_1m_inside, direction, level.price,
            atr_1m if atr_1m > 0 else atr / 2.2,
            rolling_avg_vol_1m if rolling_avg_vol_1m > 0 else rolling_avg_vol / 5,
        )

    conf = score_signal(
        level=level,
        vol_ratio=vol_ratio,
        displacement=wick_rejection,
        wick_ratio=wick_body_ratio(candle, direction),
        cvd_ratio=cvd_ratio,
        cvd_divergence=False,
        approach=approach,
        cvd_quarantine=cvd_quarantine,
        day_bias=day_bias,
        trend_5m=trend_5m,
        signal_dir=direction,
        setup_type="FAILED_AUCTION_VAR",
        tests_today=level.tests_today,
    )

    # Add 1m enrichment bonus
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
        "setup":          "FAILED_AUCTION_VAR",
        "direction":      direction,
        "entry":          None,
        "target":         poc,
        "approach":       approach,
        "confidence":     conf,
        "vol_ratio":      vol_ratio,
        "cvd_ratio":      cvd_ratio,
        "wick_extreme":   wick_extreme,
        "wick_past":      wick_past,
        "wick_rejection": wick_rejection,
        "var_type":       var_type,
        "trend_5m":       trend_5m,
        "trend_pts":      conf.components.get("trend_5m", 0),
        "enrichment":     enrichment,
        "details":        (
            f"5m {var_type} rejection={wick_rejection:.0%} "
            f"target=POC ${poc:.2f} vol={vol_ratio:.1f}x cvd={cvd_ratio:.1f}x "
            f"trend={trend_5m} enrich=+{enrichment_bonus} conf={conf.score}"
        ),
    }


def _detect_major_level(
    candles_5m, level, atr, rolling_avg_vol, cvd_turn, rolling_avg_cvd,
    cvd_quarantine, day_bias, candles_1m=None,
    bars_1m_inside=None, atr_1m=0.0, rolling_avg_vol_1m=0.0,
) -> dict | None:
    """Setup 3B: Major Level Rejection — V4.0 5m detection.
    Trigger on 5m bar: proximity + wick/body >= 2.0.
    Uses 1m enrichment for absorption scoring.
    Level score >= 8 required."""
    if level.score < 8:
        return None
    if len(candles_5m) < 3:
        return None

    # ── PHASE 1: TRIGGER on 5m bar ──
    candle = candles_5m[-1]

    # Doji filter — bar must have directional body
    candle_body = abs(candle.c - candle.o)
    candle_range = candle.h - candle.l
    if candle_range > 0 and candle_body / candle_range < 0.12:
        return None  # doji — both wicks pass wick/body, no real rejection

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

    # Wick/body >= 2.0 on rejection side
    if near_as_resistance and candle.c < level.price and upper_ratio >= 2.0:
        direction = "BEARISH"
        wick_r = upper_ratio
        wick_extreme = candle.h
    elif near_as_support and candle.c > level.price and lower_ratio >= 2.0:
        direction = "BULLISH"
        wick_r = lower_ratio
        wick_extreme = candle.l
    else:
        return None

    # ── PHASE 2: GRADE on 5m ──
    vol_ratio = rolling_vol_ratio(candle, rolling_avg_vol)
    cvd_ratio = cvd_turn_magnitude(cvd_turn, rolling_avg_cvd)
    bar_range = candle.h - candle.l
    wick_rejection = (upper_wick if direction == "BEARISH" else lower_wick) / bar_range if bar_range > 0 else 0

    # 5m approach context
    approach = classify_approach(
        candles_5m[-6:-1] if len(candles_5m) >= 6 else candles_5m[:-1],
        level.price, atr, rolling_avg_vol,
    )

    # 5m trend
    from .metrics import get_5m_trend
    trend_5m = get_5m_trend(candles_5m) if len(candles_5m) >= 4 else "NEUTRAL"

    # 1m enrichment (reuse S1's function)
    enrichment = {"absorption": 0, "vol_cluster": 0, "cvd_micro": 0, "total": 0}
    if bars_1m_inside and len(bars_1m_inside) >= 2:
        from ..detection.liquidity_grab import score_1m_enrichment
        enrichment = score_1m_enrichment(
            bars_1m_inside, direction, level.price,
            atr_1m if atr_1m > 0 else atr / 2.2,
            rolling_avg_vol_1m if rolling_avg_vol_1m > 0 else rolling_avg_vol / 5,
        )

    conf = score_signal(
        level=level,
        vol_ratio=vol_ratio,
        displacement=wick_rejection,
        wick_ratio=wick_r,
        cvd_ratio=cvd_ratio,
        cvd_divergence=False,
        approach=approach,
        cvd_quarantine=cvd_quarantine,
        day_bias=day_bias,
        trend_5m=trend_5m,
        signal_dir=direction,
        setup_type="FAILED_AUCTION_MAJOR",
        tests_today=level.tests_today,
    )

    # Add 1m enrichment bonus
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
        "setup":      "FAILED_AUCTION_MAJOR",
        "direction":  direction,
        "entry":      None,
        "target":     None,
        "approach":   approach,
        "confidence": conf,
        "vol_ratio":  vol_ratio,
        "cvd_ratio":  cvd_ratio,
        "wick_extreme": wick_extreme,
        "wick_ratio": wick_r,
        "wick_rejection": wick_rejection,
        "trend_5m":   trend_5m,
        "trend_pts":  conf.components.get("trend_5m", 0),
        "enrichment": enrichment,
        "details":    (
            f"5m wick/body={wick_r:.1f} rejection={wick_rejection:.0%} "
            f"vol={vol_ratio:.1f}x cvd={cvd_ratio:.1f}x trend={trend_5m} "
            f"enrich=+{enrichment_bonus} conf={conf.score}"
        ),
    }
