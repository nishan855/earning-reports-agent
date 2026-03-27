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
    trs = [
        max(sl[i].h - sl[i].l,
            abs(sl[i].h - sl[i-1].c),
            abs(sl[i].l - sl[i-1].c))
        for i in range(1, len(sl))
    ]
    return sum(trs) / period


def calc_avg_vol(candles: list[Candle], period: int = AVG_VOL_PERIOD) -> float:
    if len(candles) < period + 2:
        return 0.0
    return sum(c.v for c in candles[-period-1:-1]) / period
