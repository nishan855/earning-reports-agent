from .models import Candle, Level, PreMarketData, OpeningRange
from .market_utils import detect_swings, calc_vwap


def build_levels(
    c1m:     list[Candle],
    c5m:     list[Candle],
    c_daily: list[Candle],
    pm_data: PreMarketData | None,
    or_data: OpeningRange  | None,
) -> list[Level]:
    raw: list[Level] = []
    last_price = c1m[-1].c if c1m else 0.0

    # Previous day high/low — institutional memory
    if pm_data:
        raw += [
            Level(pm_data.pd_high, "PDH", "resistance", 4, "PD"),
            Level(pm_data.pd_low,  "PDL", "support",    4, "PD"),
        ]

    # Opening range — morning direction
    if or_data and or_data.complete:
        raw += [
            Level(or_data.high, "ORH", "resistance", 4, "OR"),
            Level(or_data.low,  "ORL", "support",    4, "OR"),
        ]

    # VWAP — institutional fair value
    vwap = calc_vwap(c1m)
    if vwap:
        raw.append(Level(vwap, "VWAP", "dynamic", 4, "VWAP"))

    # Previous week high/low — only if within 2% of price
    if len(c_daily) >= 6 and last_price:
        last_week = c_daily[-6:-1]
        pwh = max(c.h for c in last_week)
        pwl = min(c.l for c in last_week)
        if abs(pwh - last_price) / last_price < 0.02:
            raw.append(Level(pwh, "PWH", "resistance", 3, "PD"))
        if abs(pwl - last_price) / last_price < 0.02:
            raw.append(Level(pwl, "PWL", "support", 3, "PD"))

    # Swing highs/lows from 5m — only nearest above and below
    if len(c5m) > 10 and last_price:
        highs, lows = detect_swings(c5m[:-1], lb=3)
        above_swings = sorted([h for h in highs if h["price"] > last_price], key=lambda h: h["price"])
        below_swings = sorted([l for l in lows if l["price"] < last_price], key=lambda l: l["price"], reverse=True)
        if above_swings:
            raw.append(Level(above_swings[0]["price"], "Swing H", "resistance", 2, "SWING"))
        if below_swings:
            raw.append(Level(below_swings[0]["price"], "Swing L", "support", 2, "SWING"))

    raw = [l for l in raw if l.price > 0]
    raw.sort(key=lambda l: l.price)

    # Dedup levels within $0.25 — keep stronger one
    deduped: list[Level] = []
    for lvl in raw:
        if not deduped or abs(lvl.price - deduped[-1].price) > 0.25:
            deduped.append(lvl)
        elif lvl.strength > deduped[-1].strength:
            deduped[-1] = lvl

    return deduped


def compute_opening_range(c1m: list[Candle]) -> OpeningRange | None:
    from .sessions import get_or_start_ts, get_or_end_ts, is_or_complete
    start = get_or_start_ts()
    end   = get_or_end_ts()
    bars  = [c for c in c1m if start <= c.t < end]
    if not bars:
        return None
    return OpeningRange(
        high=max(c.h for c in bars),
        low=min(c.l for c in bars),
        complete=is_or_complete(),
        bar_count=len(bars),
    )
