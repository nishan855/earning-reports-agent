import pytz
from datetime import datetime
from ..models import Candle, DayContext

ET = pytz.timezone("America/New_York")


def compute_day_bias(
    daily_bars: list,
    bars_15m: list,
    current_price: float,
    or_high: float,
    or_low: float,
    vwap: float,
) -> tuple:
    """
    Returns (bias: str, score: int).
    bias: BULLISH | BEARISH | NEUTRAL
    score: integer
    Locked at 10:00 AM.
    """
    score = 0

    # Daily trend
    if len(daily_bars) >= 5:
        last5 = daily_bars[-5:]
        if last5[-1].c > last5[0].c:
            score += 1
        elif last5[-1].c < last5[0].c:
            score -= 1

    # 15m trend
    if len(bars_15m) >= 4:
        last4 = bars_15m[-4:]
        if last4[-1].c > last4[0].c:
            score += 1
        elif last4[-1].c < last4[0].c:
            score -= 1

    # Broke above ORH or below ORL
    if or_high > 0 and current_price > or_high:
        score += 2
    elif or_low > 0 and current_price < or_low:
        score -= 2

    # Price vs VWAP
    if vwap > 0:
        if current_price > vwap:
            score += 1
        elif current_price < vwap:
            score -= 1

    if score >= 3:
        bias = "BULLISH"
    elif score <= -3:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    return bias, score


def compute_day_type(
    bars_1m_today: list,
    atr: float = 0.0,
    current_price: float = 0.0,
    pd_vah: float = 0.0,
    pd_val: float = 0.0,
    vwap: float = 0.0,
) -> str:
    """V3.2: TREND | RANGE — Displacement + Institutional Value filter.

    Two-factor authentication:
    1. ATR Displacement: net move over last 30 bars must exceed ATR × 1.5
    2. Institutional Value: price must be outside pdVAH/pdVAL (or above/below VWAP as fallback)

    Both must pass → TREND. Otherwise → RANGE.
    """
    if len(bars_1m_today) < 10:
        return "RANGE"

    # ── Factor 1: ATR Displacement Check ──
    sample = bars_1m_today[-30:]
    if atr <= 0:
        # Fallback: compute ATR from sample if not provided
        trs = [max(c.h - c.l, abs(c.h - sample[i-1].c), abs(c.l - sample[i-1].c))
               for i, c in enumerate(sample) if i > 0]
        atr = sum(trs) / len(trs) if trs else 0.001

    net_displacement = abs(sample[-1].c - sample[0].c)
    if net_displacement <= atr * 1.5:
        return "RANGE"

    # ── Factor 2: Institutional Value Filter ──
    price = current_price or sample[-1].c

    if pd_vah > 0 and pd_val > 0:
        # Primary: price must be outside prior day value area
        outside_value = price > pd_vah or price < pd_val
    elif vwap > 0:
        # Fallback: price clearly holding one side of VWAP
        # "Clearly" = at least 0.1% away from VWAP to avoid noise
        vwap_margin = vwap * 0.001
        outside_value = price > (vwap + vwap_margin) or price < (vwap - vwap_margin)
    else:
        return "RANGE"

    return "TREND" if outside_value else "RANGE"


def assess_day_context(
    asset: str, daily_bars: list[Candle], bars_15m: list[Candle],
    bars_1m_today: list[Candle], or_high: float, or_low: float,
    current_price: float, benchmark_price: float = 0.0, benchmark_prev: float = 0.0,
    atr: float = 0.0, pd_vah: float = 0.0, pd_val: float = 0.0, vwap: float = 0.0,
) -> DayContext:
    gap_pct, gap_type, gap_filled = 0.0, "FLAT", False
    if len(daily_bars) >= 1 and bars_1m_today:
        prev_close = daily_bars[-1].c  # last completed daily bar = yesterday's close
        today_open = bars_1m_today[0].o  # first intraday bar = today's actual open
        if prev_close > 0:
            gap_pct = ((today_open - prev_close) / prev_close) * 100
            if gap_pct > 0.3: gap_type = "GAP_UP"
            elif gap_pct < -0.3: gap_type = "GAP_DOWN"
            if gap_type == "GAP_UP": gap_filled = current_price <= prev_close
            elif gap_type == "GAP_DOWN": gap_filled = current_price >= prev_close

    day_type = compute_day_type(
        bars_1m_today, atr=atr, current_price=current_price,
        pd_vah=pd_vah, pd_val=pd_val, vwap=vwap,
    )

    bias_points = 0
    if len(daily_bars) >= 5:
        last5 = daily_bars[-5:]
        if last5[-1].c > last5[0].c and last5[-1].l > last5[1].l: bias_points += 1
        elif last5[-1].c < last5[0].c and last5[-1].h < last5[1].h: bias_points -= 1
    if len(bars_15m) >= 4:
        if bars_15m[-1].c > bars_15m[-4].c: bias_points += 1
        elif bars_15m[-1].c < bars_15m[-4].c: bias_points -= 1
    if or_high > 0 and or_low > 0:
        if current_price > or_high: bias_points += 2
        elif current_price < or_low: bias_points -= 2
        elif current_price > (or_high + or_low) / 2: bias_points += 1
        else: bias_points -= 1

    bias = "BULLISH" if bias_points >= 2 else "BEARISH" if bias_points <= -2 else "NEUTRAL"

    rel_str = 0.0
    if benchmark_price > 0 and benchmark_prev > 0 and daily_bars:
        asset_chg = ((current_price - daily_bars[-1].o) / daily_bars[-1].o) * 100
        bench_chg = ((benchmark_price - benchmark_prev) / benchmark_prev) * 100
        rel_str = asset_chg - bench_chg

    return DayContext(
        asset=asset, day_type=day_type, bias=bias, bias_locked=True,
        gap_pct=round(gap_pct, 2), gap_type=gap_type, gap_filled=gap_filled,
        relative_str=round(rel_str, 2), or_high=or_high, or_low=or_low, or_complete=True,
    )
