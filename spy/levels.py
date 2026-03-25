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

    if pm_data:
        raw += [
            Level(pm_data.pd_high,  "PDH",      "resistance", 4, "PD"),
            Level(pm_data.pd_low,   "PDL",      "support",    4, "PD"),
            Level(pm_data.pd_close, "PDC",      "pivot",      3, "PD"),
            Level(pm_data.pm_high,  "PM High",  "resistance", 3, "PM"),
            Level(pm_data.pm_low,   "PM Low",   "support",    3, "PM"),
        ]
        if pm_data.gap_fill:
            raw.append(Level(pm_data.gap_fill, "Gap Fill", "pivot", 2, "PD"))

    if or_data and or_data.complete:
        raw += [
            Level(or_data.high, "ORH", "resistance", 4, "OR"),
            Level(or_data.low,  "ORL", "support",    4, "OR"),
        ]

    vwap = calc_vwap(c1m)
    if vwap:
        raw.append(Level(vwap, "VWAP", "dynamic", 3, "VWAP"))

    if len(c_daily) >= 6:
        last_week = c_daily[-6:-1]
        raw += [
            Level(max(c.h for c in last_week), "PWH", "resistance", 3, "PD"),
            Level(min(c.l for c in last_week), "PWL", "support",    3, "PD"),
        ]

    if last_price:
        lo = int(last_price * 0.97)
        hi = int(last_price * 1.03) + 1
        for r in range(lo, hi + 1):
            dist_pct = abs(r - last_price) / last_price
            if dist_pct > 0.03:
                continue
            if r % 10 == 0:
                raw.append(Level(float(r), f"${r}", "round", 3, "ROUND"))
            elif r % 5 == 0:
                raw.append(Level(float(r), f"${r}", "round", 2, "ROUND"))
            else:
                raw.append(Level(float(r), f"${r}", "round", 1, "ROUND"))
        half = round(last_price * 2) / 2
        if abs(half - last_price) < 3 and half != int(half):
            raw.append(Level(half, f"${half:.1f}", "round", 2, "ROUND"))

    if len(c5m) > 10:
        highs, lows = detect_swings(c5m[:-1], lb=3)
        for h in highs[-5:]:
            raw.append(Level(h["price"], "Swing H", "resistance", 2, "SWING"))
        for l in lows[-5:]:
            raw.append(Level(l["price"], "Swing L", "support",    2, "SWING"))

    raw = [l for l in raw if l.price > 0]
    raw.sort(key=lambda l: l.price)

    deduped: list[Level] = []
    for lvl in raw:
        if not deduped or abs(lvl.price - deduped[-1].price) > 0.25:
            deduped.append(lvl)
        elif lvl.strength > deduped[-1].strength:
            deduped[-1] = lvl

    return deduped


def find_nearest_levels(
    price: float,
    levels: list[Level],
) -> tuple[Level | None, Level | None]:
    buffer = 0.10
    above  = sorted([l for l in levels if l.price > price + buffer], key=lambda l: l.price)
    below  = sorted([l for l in levels if l.price < price - buffer], key=lambda l: l.price, reverse=True)
    return (above[0] if above else None), (below[0] if below else None)


def calc_rr(
    price: float,
    bias: str,
    atr: float,
    resistance: Level | None,
    support: Level | None,
) -> dict:
    if not atr or not price:
        return {"rr": 0, "sl": 0, "tp1": 0, "tp2": 0}

    sl_buffer = atr * 0.5
    sl   = price - atr * 1.5 if bias == "BULLISH" else price + atr * 1.5
    risk = abs(price - sl)

    if bias == "BULLISH":
        tp1 = (resistance.price - sl_buffer) if resistance else price + atr * 2
    else:
        tp1 = (support.price + sl_buffer) if support else price - atr * 2

    tp2 = price + atr * 3.5 if bias == "BULLISH" else price - atr * 3.5

    reward = abs(tp1 - price)
    rr = round(reward / risk, 2) if risk > 0 else 0

    return {"rr": rr, "sl": round(sl, 2), "tp1": round(tp1, 2), "tp2": round(tp2, 2)}


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
