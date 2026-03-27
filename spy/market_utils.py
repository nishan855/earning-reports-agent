from .models import Candle, TrendDirection

MIN_RR         = 2.0
ATR_PERIOD     = 14
AVG_VOL_PERIOD = 20


def detect_swings(candles: list[Candle], lb: int = 5):
    highs, lows = [], []
    for i in range(lb, len(candles) - lb):
        is_high = all(candles[j].h < candles[i].h for j in range(i-lb, i+lb+1) if j != i)
        is_low  = all(candles[j].l > candles[i].l for j in range(i-lb, i+lb+1) if j != i)
        if is_high: highs.append({"i": i, "price": candles[i].h, "t": candles[i].t})
        if is_low:  lows.append( {"i": i, "price": candles[i].l, "t": candles[i].t})
    return highs, lows


def detect_trend(candles: list[Candle]) -> TrendDirection:
    if len(candles) < 30:
        return TrendDirection.UNKNOWN
    highs, lows = detect_swings(candles, lb=5)
    if len(highs) < 2 or len(lows) < 2:
        return TrendDirection.RANGING
    ph, lh = highs[-2], highs[-1]
    pl, ll = lows[-2],  lows[-1]
    if lh["price"] > ph["price"] and ll["price"] > pl["price"]:
        return TrendDirection.BULLISH
    if lh["price"] < ph["price"] and ll["price"] < pl["price"]:
        return TrendDirection.BEARISH
    return TrendDirection.RANGING


def detect_trend_with_strength(candles: list[Candle]) -> tuple[TrendDirection, int, str]:
    """Returns (direction, bar_count, strength_label)."""
    direction = detect_trend(candles)
    if direction in (TrendDirection.RANGING, TrendDirection.UNKNOWN):
        return direction, 0, "no trend"
    highs, lows = detect_swings(candles, lb=5)
    if len(lows) < 2:
        return direction, 1, "weak"
    # Count consecutive swings in trend direction
    count = 0
    if direction == TrendDirection.BULLISH:
        for i in range(len(lows) - 1, 0, -1):
            if lows[i]["price"] > lows[i - 1]["price"]:
                count += 1
            else:
                break
    else:
        for i in range(len(highs) - 1, 0, -1):
            if highs[i]["price"] < highs[i - 1]["price"]:
                count += 1
            else:
                break
    if count <= 1:
        label = "weak"
    elif count <= 3:
        label = "moderate"
    else:
        label = "strong"
    return direction, count, label


def calc_vwap(candles: list[Candle]) -> float:
    if not candles:
        return 0.0
    tp_vol = sum(((c.h + c.l + c.c) / 3) * c.v for c in candles)
    vol    = sum(c.v for c in candles)
    return tp_vol / vol if vol > 0 else 0.0


def calc_atr(candles: list[Candle], period: int = ATR_PERIOD) -> float:
    if len(candles) < period + 1:
        return 0.0
    sl = candles[-(period + 1):]
    trs = []
    for i in range(1, len(sl)):
        tr = max(sl[i].h - sl[i].l,
                 abs(sl[i].h - sl[i-1].c),
                 abs(sl[i].l - sl[i-1].c))
        # Filter out overnight gaps (TR > 5x the bar's own range)
        bar_range = sl[i].h - sl[i].l
        if bar_range > 0 and tr > bar_range * 5:
            tr = bar_range
        trs.append(tr)
    return sum(trs) / period if trs else 0.0


def calc_avg_vol(candles: list[Candle], period: int = AVG_VOL_PERIOD) -> float:
    if len(candles) < period + 2:
        return 0.0
    return sum(c.v for c in candles[-period-1:-1]) / period
