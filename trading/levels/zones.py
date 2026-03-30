from ..models import Candle, Zone
from ..constants import (
    ZONE_LOOKBACK_DAYS, ZONE_SWING_BARS, ZONE_MIN_TESTS,
    ZONE_CLUSTER_PCT, SCORE_ZONE_STRONG, SCORE_ZONE_MEDIUM, SCORE_ZONE_WEAK,
)


def detect_zones(daily_bars: list[Candle], current_price: float, proximity_pct: float = 0.05) -> list[Zone]:
    if len(daily_bars) < ZONE_SWING_BARS * 2 + 1:
        return []

    bars = daily_bars[-ZONE_LOOKBACK_DAYS:]
    swing_highs = _find_swing_points(bars, "high")
    swing_lows = _find_swing_points(bars, "low")

    swing_highs = [s for s in swing_highs if abs(s["price"] - current_price) / current_price <= proximity_pct]
    swing_lows = [s for s in swing_lows if abs(s["price"] - current_price) / current_price <= proximity_pct]

    resistance_zones = _cluster_swings(swing_highs, "resistance")
    support_zones = _cluster_swings(swing_lows, "support")

    all_zones = []
    for cluster in resistance_zones + support_zones:
        if len(cluster["points"]) < ZONE_MIN_TESTS:
            continue
        scored = _score_zone(cluster, bars)
        if scored:
            all_zones.append(scored)

    all_zones.sort(key=lambda z: z.score, reverse=True)
    return all_zones


def _find_swing_points(bars: list[Candle], point_type: str) -> list[dict]:
    swings = []
    lb = ZONE_SWING_BARS
    for i in range(lb, len(bars) - lb):
        bar = bars[i]
        if point_type == "high":
            price = bar.h
            is_swing = all(bars[j].h <= price for j in range(i - lb, i + lb + 1) if j != i)
        else:
            price = bar.l
            is_swing = all(bars[j].l >= price for j in range(i - lb, i + lb + 1) if j != i)
        if is_swing:
            swings.append({"price": price, "bar_idx": i, "bar": bar, "type": point_type})
    return swings


def _cluster_swings(swings: list[dict], zone_type: str) -> list[dict]:
    if not swings:
        return []
    sorted_swings = sorted(swings, key=lambda s: s["price"])
    clusters = []
    current = [sorted_swings[0]]
    for swing in sorted_swings[1:]:
        cluster_mid = sum(s["price"] for s in current) / len(current)
        dist_pct = abs(swing["price"] - cluster_mid) / cluster_mid if cluster_mid > 0 else 1
        if dist_pct <= ZONE_CLUSTER_PCT:
            current.append(swing)
        else:
            clusters.append({"points": current, "zone_type": zone_type})
            current = [swing]
    clusters.append({"points": current, "zone_type": zone_type})
    return clusters


def _score_zone(cluster: dict, bars: list[Candle]) -> Zone | None:
    points = cluster["points"]
    zone_type = cluster["zone_type"]
    if not points:
        return None

    prices = [p["price"] for p in points]
    zone_low = min(prices)
    zone_high = max(prices)
    zone_mid = sum(prices) / len(prices)
    n = len(points)
    score = min(n * 2, 6)

    last_bar_idx = max(p["bar_idx"] for p in points)
    days_from_end = len(bars) - 1 - last_bar_idx
    if days_from_end < 14:
        score += 3
    elif days_from_end < 28:
        score += 2
    elif days_from_end < 90:
        score += 1

    rejections = []
    for p in points:
        idx = p["bar_idx"]
        if idx + 5 < len(bars):
            test_price = p["price"]
            future_bars = bars[idx + 1:idx + 6]
            if zone_type == "resistance":
                moves = [(test_price - b.l) / test_price for b in future_bars if test_price > 0]
            else:
                moves = [(b.h - test_price) / test_price for b in future_bars if test_price > 0]
            max_move = max(moves) if moves else 0
            rejections.append(max_move * 100)
    avg_rejection = sum(rejections) / len(rejections) if rejections else 0

    if avg_rejection > 3.0:
        score += 3
    elif avg_rejection > 1.5:
        score += 2
    elif avg_rejection > 0.5:
        score += 1

    all_vols = [b.v for b in bars if b.v > 0]
    avg_vol = sum(all_vols) / len(all_vols) if all_vols else 1
    avg_vol_ratios = [p["bar"].v / avg_vol for p in points if avg_vol > 0]
    avg_vol_ratio = sum(avg_vol_ratios) / len(avg_vol_ratios) if avg_vol_ratios else 1.0

    if avg_vol_ratio > 1.5:
        score += 2
    elif avg_vol_ratio > 1.2:
        score += 1

    if score >= 10:
        level_score = SCORE_ZONE_STRONG
    elif score >= 7:
        level_score = SCORE_ZONE_MEDIUM
    elif score >= 4:
        level_score = SCORE_ZONE_WEAK
    else:
        return None

    return Zone(
        zone_low=round(zone_low, 2), zone_high=round(zone_high, 2), zone_mid=round(zone_mid, 2),
        test_count=n, last_test_days=days_from_end, avg_rejection=round(avg_rejection, 2),
        avg_vol_ratio=round(avg_vol_ratio, 2), score=level_score, direction=zone_type,
    )
