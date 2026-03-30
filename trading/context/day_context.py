import pytz
from datetime import datetime
from ..models import Candle, DayContext

ET = pytz.timezone("America/New_York")


def assess_day_context(
    asset: str, daily_bars: list[Candle], bars_15m: list[Candle],
    bars_1m_today: list[Candle], or_high: float, or_low: float,
    current_price: float, benchmark_price: float = 0.0, benchmark_prev: float = 0.0,
) -> DayContext:
    gap_pct, gap_type, gap_filled = 0.0, "FLAT", False
    if len(daily_bars) >= 2:
        prev_close = daily_bars[-2].c
        today_open = daily_bars[-1].o
        if prev_close > 0:
            gap_pct = ((today_open - prev_close) / prev_close) * 100
            if gap_pct > 0.3: gap_type = "GAP_UP"
            elif gap_pct < -0.3: gap_type = "GAP_DOWN"
            if gap_type == "GAP_UP": gap_filled = current_price <= prev_close
            elif gap_type == "GAP_DOWN": gap_filled = current_price >= prev_close

    day_type = "RANGE"
    if len(bars_1m_today) >= 10:
        changes = sum(1 for i in range(1, min(30, len(bars_1m_today)))
                      if (bars_1m_today[i-1].c > bars_1m_today[i-1].o) != (bars_1m_today[i].c > bars_1m_today[i].o))
        day_type = "TREND" if changes <= 4 else "RANGE"

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
