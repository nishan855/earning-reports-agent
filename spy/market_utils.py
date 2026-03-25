from .models import Candle, TrendDirection, StopHunt, PriceAction, Level
from typing import Optional

LEVEL_PROXIMITY_PCT = 0.003
MIN_RR              = 2.0
ATR_PERIOD          = 14
AVG_VOL_PERIOD      = 20


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


def detect_bos(candles: list[Candle]) -> Optional[dict]:
    if len(candles) < 15:
        return None
    highs, lows = detect_swings(candles, lb=3)
    if not highs or not lows:
        return None
    last_h = highs[-1]
    last_l = lows[-1]
    last   = candles[-1]
    if last.c > last_h["price"]:
        return {"type": "BULLISH", "level": last_h["price"]}
    if last.c < last_l["price"]:
        return {"type": "BEARISH", "level": last_l["price"]}
    return None


def detect_stop_hunt(candles: list[Candle]) -> Optional[StopHunt]:
    if len(candles) < 12:
        return None
    recent = candles[-20:]
    last   = candles[-1]
    highs, lows = detect_swings(recent, lb=2)
    if not lows or not highs:
        return None

    body = abs(last.c - last.o)

    if lows:
        recent_low = lows[-1]
        wick_down  = min(last.o, last.c) - last.l
        if (last.l < recent_low["price"] and
            last.c > recent_low["price"] and
            last.c > last.o and
            wick_down > body * 1.5):
            return StopHunt(type="BULLISH", level=recent_low["price"], wick_size=wick_down)

    if highs:
        recent_high = highs[-1]
        wick_up     = last.h - max(last.o, last.c)
        if (last.h > recent_high["price"] and
            last.c < recent_high["price"] and
            last.c < last.o and
            wick_up > body * 1.5):
            return StopHunt(type="BEARISH", level=recent_high["price"], wick_size=wick_up)

    return None


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


def detect_price_action(
    last_closed: Candle,
    prev_closed: Candle,
    levels: list[Level],
    bias: TrendDirection,
) -> Optional[PriceAction]:
    for lvl in levels:
        prox = abs(last_closed.c - lvl.price) / last_closed.c
        if prox > LEVEL_PROXIMITY_PCT:
            continue

        body   = abs(last_closed.c - last_closed.o)
        wick_u = last_closed.h - max(last_closed.o, last_closed.c)
        wick_d = min(last_closed.o, last_closed.c) - last_closed.l

        if bias == TrendDirection.BULLISH and last_closed.c > lvl.price and prev_closed.c < lvl.price and last_closed.c > last_closed.o:
            return PriceAction(type="BREAKOUT", level=lvl, strength="HIGH")
        if bias == TrendDirection.BEARISH and last_closed.c < lvl.price and prev_closed.c > lvl.price and last_closed.c < last_closed.o:
            return PriceAction(type="BREAKOUT", level=lvl, strength="HIGH")

        touch_below = abs(last_closed.l - lvl.price) / last_closed.c < 0.001
        touch_above = abs(last_closed.h - lvl.price) / last_closed.c < 0.001
        if bias == TrendDirection.BULLISH and touch_below and last_closed.c > lvl.price:
            return PriceAction(type="RETEST", level=lvl, strength="HIGH")
        if bias == TrendDirection.BEARISH and touch_above and last_closed.c < lvl.price:
            return PriceAction(type="RETEST", level=lvl, strength="HIGH")

        if bias == TrendDirection.BEARISH and wick_u > body * 1.5 and last_closed.c < last_closed.o:
            return PriceAction(type="REJECTION", level=lvl, strength="MEDIUM")
        if bias == TrendDirection.BULLISH and wick_d > body * 1.5 and last_closed.c > last_closed.o:
            return PriceAction(type="REJECTION", level=lvl, strength="MEDIUM")

    return None
