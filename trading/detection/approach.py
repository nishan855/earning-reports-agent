"""
Approach context classifier.
Evaluates the 5 closed candles leading up to the level.
Returns the FIRST matching type in priority order.
"""

from dataclasses import dataclass
from ..models import Candle


@dataclass
class ApproachResult:
    type: str          # AGGRESSIVE_PUSH | ABSORPTION | EXHAUSTION | MOMENTUM | NEUTRAL
    confidence_pts: int  # contribution to confidence score
    details: str


def classify_approach(
    candles: list,        # last 5+ closed candles (index -1 = most recent)
    level_price: float,
    atr: float,
    rolling_avg_vol: float,  # rolling_avg(last_10_bars)
) -> ApproachResult:
    """
    Priority order:
    1. AGGRESSIVE_PUSH   (displacement-grade candle present — unmistakable)
    2. ABSORPTION        (volume is tie-breaker vs exhaustion — check first)
    3. EXHAUSTION
    4. MOMENTUM
    5. NEUTRAL           (default — detection not blocked, confidence = 0)
    """
    if len(candles) < 4:
        return ApproachResult("NEUTRAL", 0, "Insufficient candles")

    c = candles  # use negative indexing throughout
    bodies  = [abs(x.c - x.o) for x in c]
    volumes = [x.v for x in c]
    closes  = [x.c for x in c]
    rvol    = rolling_avg_vol if rolling_avg_vol > 0 else 1.0

    # ── 1. AGGRESSIVE PUSH ──────────────────────────────────────────
    # Requires a strong directional candle in last 2 bars
    recent_bodies = bodies[-2:]
    has_displacement = any(b > atr * 0.6 for b in recent_bodies)

    if has_displacement:
        big_idx = -1 if bodies[-1] > atr * 0.6 else -2
        big_c   = c[big_idx]
        vol_ratio = big_c.v / rvol

        # Direction toward level
        moving_toward = (
            (big_c.c > big_c.o and big_c.c >= level_price * 0.995)
            or
            (big_c.c < big_c.o and big_c.c <= level_price * 1.005)
        )
        price_progress = abs(closes[-1] - closes[-3]) > atr * 0.5

        if vol_ratio >= 1.5 and moving_toward and price_progress:
            return ApproachResult(
                "AGGRESSIVE_PUSH", 12,
                f"Displacement candle body={bodies[big_idx]:.2f} vol={vol_ratio:.1f}x"
            )

    # ── 2. ABSORPTION (check before exhaustion — volume is tie-breaker)
    price_range    = max(x.h for x in c[-4:]) - min(x.l for x in c[-4:])
    tight_range    = price_range < atr * 1.2
    high_vol_count = sum(1 for v in volumes[-4:] if v > rvol * 0.8)
    avg_vol_4      = sum(volumes[-4:]) / 4 if len(volumes) >= 4 else 0
    elevated_vol   = high_vol_count >= 2 and avg_vol_4 > rvol * 1.0

    if tight_range and elevated_vol:
        return ApproachResult(
            "ABSORPTION", 15,
            f"Range={price_range:.2f} (<ATR*1.2={atr*1.2:.2f}), vol_count={high_vol_count}/4"
        )

    # ── 3. EXHAUSTION ────────────────────────────────────────────────
    if len(bodies) >= 3:
        shrinking = (
            bodies[-1] < bodies[-2] < bodies[-3]
            and all(b < atr * 0.5 for b in bodies[-3:])
        )
        avg_vol_last3 = sum(volumes[-3:]) / 3 if len(volumes) >= 3 else 0
        avg_vol_prev3 = sum(volumes[-6:-3]) / 3 if len(volumes) >= 6 else rvol
        vol_flat      = avg_vol_last3 <= avg_vol_prev3 * 1.3

        if shrinking and vol_flat:
            return ApproachResult(
                "EXHAUSTION", 15,
                f"bodies={[round(b,2) for b in bodies[-3:]]} vol_trend=flat"
            )

    # ── 4. MOMENTUM ──────────────────────────────────────────────────
    if len(closes) >= 5:
        bullish_count = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
        bearish_count = sum(1 for i in range(1, len(closes)) if closes[i] < closes[i-1])
        directional   = bullish_count >= 3 or bearish_count >= 3

        total_progress = abs(closes[-1] - closes[-5]) > atr * 0.3

        if directional and total_progress:
            direction = "bull" if bullish_count >= 3 else "bear"
            return ApproachResult(
                "MOMENTUM", 10,
                f"{direction} {max(bullish_count, bearish_count)}/5 bars, progress={abs(closes[-1]-closes[-5]):.2f}"
            )

    return ApproachResult("NEUTRAL", 0, "No clear approach pattern")
