"""
Setup 2: Institutional Order Block Defense — V4.0

Continuation trade on TREND days. Price pulls back to an order block
(last opposite candle before displacement) and institutions defend it.

V4.0 changes:
  - OB body relaxed: ATR × 0.5 (was 1.0)
  - OB volume relaxed: 1.0× (was 1.5×)
  - OB time window: 90 min (was 60 min)
  - ABSORPTION no longer hard gate — scored instead
  - CVD no longer hard gate — scored instead
  - Uses pending_sweep → 1m momentum confirmation
  - 1m enrichment scoring
"""

from ..models import Candle, Level
from ..context.sim_clock import now_et
from .approach import classify_approach
from .metrics import displacement_ratio, rolling_vol_ratio, cvd_turn_magnitude, get_5m_trend
from .confidence import score_signal


def find_order_block(
    candles_5m: list,
    displacement_candle_idx: int,
    direction: str,
    atr: float,
    rolling_avg_vol: float,
) -> dict | None:
    """Order Block = last opposite-colored candle before displacement.
    V4.0 relaxed requirements:
      - body > ATR × 0.5 (was 1.0)
      - volume >= 1.0× avg (was 1.5×)
      - formed within 90 min (was 60 min)
    """
    if displacement_candle_idx < 1:
        return None

    ob_color = "BEARISH" if direction == "BULLISH" else "BULLISH"
    now_ts = int(now_et().timestamp() * 1000)

    for i in range(displacement_candle_idx - 1, max(-1, displacement_candle_idx - 15), -1):
        c = candles_5m[i]
        is_correct_color = (
            (ob_color == "BEARISH" and c.c < c.o) or
            (ob_color == "BULLISH" and c.c > c.o)
        )
        if not is_correct_color:
            continue

        body = abs(c.c - c.o)
        if body < atr * 0.5:
            continue

        vol_ratio = c.v / rolling_avg_vol if rolling_avg_vol > 0 else 0
        if vol_ratio < 1.0:
            continue

        # Formed within 90 min
        if now_ts - c.t > 5_400_000:
            continue

        ob_high = max(c.o, c.c)
        ob_low = min(c.o, c.c)

        return {
            "candle": c,
            "ob_high": ob_high,
            "ob_low": ob_low,
            "ob_mid": (ob_high + ob_low) / 2,
            "vol_ratio": vol_ratio,
            "formed_at": c.t,
            "body": body,
        }

    return None


def count_ob_visits(candles_5m: list, ob_high: float, ob_low: float, ob_formed_idx: int) -> int:
    visits = 0
    for c in candles_5m[ob_formed_idx + 1:]:
        if c.l <= ob_high and c.h >= ob_low:
            visits += 1
    return visits


def detect_ob_defense(
    candles_5m: list,
    candles_1m: list,
    level: Level,
    atr: float,
    rolling_avg_vol_5m: float,
    rolling_avg_vol_1m: float,
    cvd_turn: float,
    rolling_avg_cvd: float,
    day_type: str,
    day_bias: str,
    cvd_quarantine: bool = False,
    bars_1m_inside: list = None,
    atr_1m: float = 0.0,
) -> dict | None:
    """V4.0: Detect OB defense on 5m. TREND day required.
    ABSORPTION and CVD are scored, not hard gated."""

    # Day type gate — only hard gate remaining
    if day_type != "TREND":
        return None
    if day_bias not in ("BULLISH", "BEARISH"):
        return None

    direction = day_bias
    if len(candles_5m) < 5:
        return None

    last_5m = candles_5m[-1]

    # Doji filter — defense candle must have directional body
    defense_body = abs(last_5m.c - last_5m.o)
    defense_range = last_5m.h - last_5m.l
    if defense_range > 0 and defense_body / defense_range < 0.12:
        return None  # doji — no directional defense visible


    # Approach classification — scored, not gated
    approach = classify_approach(
        candles_5m[-6:-1] if len(candles_5m) >= 6 else candles_5m[:-1],
        level.price, atr, rolling_avg_vol_5m,
    )

    # Find most recent displacement to locate OB
    displacement_idx = len(candles_5m) - 1
    ob = find_order_block(candles_5m, displacement_idx, direction, atr, rolling_avg_vol_5m)
    if not ob:
        return None

    # OB visit count — max 3 (relaxed from 2)
    ob_formed_idx = next((i for i, c in enumerate(candles_5m) if c.t == ob["formed_at"]), 0)
    visits = count_ob_visits(candles_5m, ob["ob_high"], ob["ob_low"], ob_formed_idx)
    if visits >= 3:
        return None

    # Proximity: price within ATR × 0.3 of OB zone (relaxed from 0.2)
    proximity = atr * 0.3
    if direction == "BULLISH":
        touched_ob = last_5m.l <= ob["ob_high"] + proximity
    else:
        touched_ob = last_5m.h >= ob["ob_low"] - proximity
    if not touched_ob:
        return None

    # Wick extreme for stop placement
    if direction == "BULLISH":
        wick_extreme = last_5m.l
    else:
        wick_extreme = last_5m.h

    # Score
    cvd_ratio = cvd_turn_magnitude(cvd_turn, rolling_avg_cvd)
    vol_ratio = rolling_vol_ratio(last_5m, rolling_avg_vol_5m)
    bar_range = last_5m.h - last_5m.l
    disp = displacement_ratio(last_5m)
    trend_5m = get_5m_trend(candles_5m) if len(candles_5m) >= 4 else "NEUTRAL"

    # 1m enrichment
    enrichment = {"absorption": 0, "vol_cluster": 0, "cvd_micro": 0, "total": 0}
    if bars_1m_inside and len(bars_1m_inside) >= 2:
        from ..detection.liquidity_grab import score_1m_enrichment
        enrichment = score_1m_enrichment(
            bars_1m_inside, direction, level.price,
            atr_1m if atr_1m > 0 else atr / 2.2,
            rolling_avg_vol_1m if rolling_avg_vol_1m > 0 else rolling_avg_vol_5m / 5,
        )

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
        trend_5m=trend_5m,
        signal_dir=direction,
        setup_type="OB_DEFENSE",
        tests_today=level.tests_today,
    )

    # Add enrichment bonus
    enrichment_bonus = enrichment.get("total", 0)
    conf.score = min(100, conf.score + enrichment_bonus)
    if conf.score >= 75:
        conf.label = "HIGH"
    elif conf.score >= conf.threshold:
        conf.label = "MEDIUM"

    if conf.score < conf.threshold:
        return None

    return {
        "setup":        "OB_DEFENSE",
        "direction":    direction,
        "entry":        None,
        "approach":     approach,
        "confidence":   conf,
        "ob":           ob,
        "ob_visits":    visits,
        "vol_ratio":    vol_ratio,
        "cvd_ratio":    cvd_ratio,
        "wick_extreme": wick_extreme,
        "trend_5m":     trend_5m,
        "trend_pts":    conf.components.get("trend_5m", 0),
        "enrichment":   enrichment,
        "details":      (
            f"5m OB ${ob['ob_low']:.2f}-${ob['ob_high']:.2f} "
            f"visits={visits} approach={approach.type} "
            f"vol={vol_ratio:.1f}x cvd={cvd_ratio:.1f}x trend={trend_5m} "
            f"enrich=+{enrichment_bonus} conf={conf.score}"
        ),
    }
