"""
BACKTEST ENGINE — Tests signal detection against real historical data.
For each scenario, simulates what the system would have detected at that
time, then checks what actually happened next to determine win/loss.

Uses real yfinance data. No mocking.
"""
import sys
sys.path.insert(0, ".")

import yfinance as yf
from trading.models import Candle, Level
from trading.levels.builder import build_levels, calc_vwap
from trading.levels.volume_profile import compute_volume_profile
from trading.levels.zones import detect_zones
from trading.detection.breakout import detect_breakout
from trading.detection.rejection import detect_rejection
from trading.detection.stop_hunt import detect_stop_hunt
from trading.context.day_context import assess_day_context
from trading.context.options_context import get_options_env
from trading.constants import MIN_RR
from datetime import datetime, timedelta
import pytz

ET = pytz.timezone("America/New_York")


def to_candles(df):
    out = []
    for ts, row in df.iterrows():
        try:
            c = Candle(t=int(ts.timestamp() * 1000),
                       o=round(float(row["Open"]), 2), h=round(float(row["High"]), 2),
                       l=round(float(row["Low"]), 2), c=round(float(row["Close"]), 2),
                       v=float(row["Volume"]))
            if c.c > 0 and c.h >= c.l:
                out.append(c)
        except:
            pass
    return out


def filter_time_range(candles, start_min, end_min):
    """Filter candles between start_min and end_min (minutes from midnight ET)."""
    out = []
    for c in candles:
        et = datetime.fromtimestamp(c.t / 1000, tz=ET)
        t = et.hour * 60 + et.minute
        if start_min <= t < end_min:
            out.append(c)
    return out


def compute_cvd_approx(candles):
    """Approximate CVD from bar data."""
    cvd = 0.0
    for c in candles:
        rng = c.h - c.l
        if rng > 0:
            ratio = (c.c - c.l) / rng
            cvd += c.v * (ratio - 0.5) * 2
    return cvd


def run_scenario(asset, c1m_full, c5m_full, c15m, daily, scenario_time_min, scenario_name):
    """
    Run detection at scenario_time_min, then check what happened
    in the next 30-60 minutes to validate.
    """
    # Split bars: before scenario time and after
    c1m_before = filter_time_range(c1m_full, 570, scenario_time_min)
    c1m_after = filter_time_range(c1m_full, scenario_time_min, scenario_time_min + 60)
    c5m_before = filter_time_range(c5m_full, 570, scenario_time_min)

    if len(c1m_before) < 10 or len(c1m_after) < 5:
        return None

    price = c1m_before[-1].c
    open_price = c1m_before[0].o

    # OR from first 30 min
    or_bars = filter_time_range(c1m_before, 570, 600)
    or_h = max(c.h for c in or_bars) if or_bars else 0
    or_l = min(c.l for c in or_bars) if or_bars else 0

    # VWAP, volume profile
    vwap = calc_vwap(c1m_before)
    vp = compute_volume_profile(asset, c1m_before) if len(c1m_before) >= 5 else None
    zones = detect_zones(daily, price)

    # Build levels
    levels = build_levels(asset, daily, c1m_before, c5m_before, price, vwap, or_h, or_l,
                          scenario_time_min >= 600, vp, zones)

    if not levels:
        return None

    # CVD
    cvd = compute_cvd_approx(c1m_before)
    total_vol = sum(c.v for c in c1m_before)
    cvd_bias = "BUYERS" if cvd > total_vol * 0.005 else "SELLERS" if cvd < -total_vol * 0.005 else "NEUTRAL"

    # ATR
    atr_bars = c1m_before[-14:]
    atr = sum(c.h - c.l for c in atr_bars) / len(atr_bars) if atr_bars else 1.0

    # Avg volume
    avg_vol_5m = sum(c.v for c in c5m_before[-20:]) / max(len(c5m_before[-20:]), 1) if c5m_before else 1

    # Day context
    dc = assess_day_context(asset, daily, c15m, c1m_before, or_h, or_l, price)

    # Scan for patterns on last 5m candles
    signals_found = []
    if len(c5m_before) >= 2:
        candle = c5m_before[-1]
        prev = c5m_before[-2]
        cvd_ch = candle.v * 0.6 * (1 if candle.c > candle.o else -1)

        for level in levels:
            if level.score < 6:
                continue

            # For backtesting with bar data, use volume-scaled CVD
            # Real ticks: cvd_change ~ hundreds to thousands
            # Bar approx: cvd_change ~ millions
            # Scale down to match real tick magnitudes
            cvd_scaled = cvd_ch / 1000  # approximate tick-scale
            avg_vol_scaled = avg_vol_5m / 1000

            # Breakout
            is_bo, bo_dir = detect_breakout(candle, prev, level, avg_vol_scaled, 0, cvd_scaled, atr)
            if is_bo:
                signals_found.append({
                    "pattern": "BREAKOUT", "direction": bo_dir,
                    "level": level.name, "level_price": level.price,
                    "score": level.score,
                })

            # Rejection
            is_rej, rej_dir, strength = detect_rejection(candle, level, avg_vol_scaled, 0, cvd_scaled, atr)
            if is_rej:
                signals_found.append({
                    "pattern": "REJECTION", "direction": rej_dir,
                    "level": level.name, "level_price": level.price,
                    "score": level.score, "strength": strength,
                })

    # Check what actually happened next (outcome)
    future_high = max(c.h for c in c1m_after)
    future_low = min(c.l for c in c1m_after)
    future_close = c1m_after[-1].c
    future_move = future_close - price
    future_move_pct = (future_move / price) * 100

    # For each signal, determine if it would have been profitable
    results = []
    for sig in signals_found:
        direction = sig["direction"]
        entry = price

        if direction == "BULLISH":
            stop = sig["level_price"] - atr * 0.5
            # Find nearest resistance as target
            targets = sorted([l for l in levels if l.price > price], key=lambda l: l.price)
            target = targets[0].price if targets else price + atr * 3
            risk = abs(entry - stop)
            reward = abs(target - entry)
            rr = reward / risk if risk > 0 else 0

            # Did it hit target or stop first?
            hit_target = future_high >= target
            hit_stop = future_low <= stop
            if hit_target and not hit_stop:
                outcome = "WIN"
                pnl = reward
            elif hit_stop:
                outcome = "LOSS"
                pnl = -risk
            elif future_close > entry:
                outcome = "PARTIAL_WIN"
                pnl = future_close - entry
            else:
                outcome = "PARTIAL_LOSS"
                pnl = future_close - entry

        elif direction == "BEARISH":
            stop = sig["level_price"] + atr * 0.5
            targets = sorted([l for l in levels if l.price < price], key=lambda l: l.price, reverse=True)
            target = targets[0].price if targets else price - atr * 3
            risk = abs(stop - entry)
            reward = abs(entry - target)
            rr = reward / risk if risk > 0 else 0

            hit_target = future_low <= target
            hit_stop = future_high >= stop
            if hit_target and not hit_stop:
                outcome = "WIN"
                pnl = reward
            elif hit_stop:
                outcome = "LOSS"
                pnl = -risk
            elif future_close < entry:
                outcome = "PARTIAL_WIN"
                pnl = entry - future_close
            else:
                outcome = "PARTIAL_LOSS"
                pnl = entry - future_close
        else:
            continue

        results.append({
            "scenario": scenario_name,
            "asset": asset,
            "time_min": scenario_time_min,
            "pattern": sig["pattern"],
            "direction": direction,
            "level": sig["level"],
            "level_price": sig["level_price"],
            "score": sig["score"],
            "entry": entry,
            "stop": round(stop, 2),
            "target": round(target, 2),
            "rr": round(rr, 1),
            "rr_passes": rr >= MIN_RR,
            "outcome": outcome,
            "pnl": round(pnl, 2),
            "future_high": future_high,
            "future_low": future_low,
            "future_close": future_close,
            "cvd_bias": cvd_bias,
            "day_bias": dc.bias,
            "vix_env": get_options_env(31.0)["label"],
            "aligned": dc.bias == direction,
        })

    return {
        "scenario": scenario_name,
        "asset": asset,
        "price": price,
        "levels": len(levels),
        "cvd_bias": cvd_bias,
        "day_bias": dc.bias,
        "signals_found": len(signals_found),
        "results": results,
    }


# ═══════════════════════════════════════════════════════════
# MAIN BACKTEST
# ═══════════════════════════════════════════════════════════

import time

print("=" * 80)
print("  BACKTEST — 10 SCENARIOS ON REAL HISTORICAL DATA")
print("  Testing detection accuracy against actual market outcomes")
print("=" * 80)

# Define 10 scenarios across different assets and times
SCENARIOS = [
    ("SPY", 630, "SPY 10:30 AM — Early session after OR lock"),
    ("SPY", 660, "SPY 11:00 AM — Power hour ending"),
    ("SPY", 780, "SPY 1:00 PM — Afternoon session"),
    ("SPY", 870, "SPY 2:30 PM — Late afternoon"),
    ("QQQ", 630, "QQQ 10:30 AM — Tech sector morning"),
    ("QQQ", 780, "QQQ 1:00 PM — Afternoon reversal zone"),
    ("AAPL", 660, "AAPL 11:00 AM — Post opening drive"),
    ("NVDA", 660, "NVDA 11:00 AM — Volatile chip stock"),
    ("TSLA", 630, "TSLA 10:30 AM — Momentum stock"),
    ("MSFT", 780, "MSFT 1:00 PM — Steady large cap"),
]

# Fetch data for all needed assets
print("\nFetching data...")
asset_data = {}
needed_assets = list(set(s[0] for s in SCENARIOS))

for asset in needed_assets:
    print(f"  {asset}...", end=" ", flush=True)
    t = yf.Ticker(asset)
    c1m = to_candles(t.history(period="1d", interval="1m"))
    c5m = to_candles(t.history(period="5d", interval="5m"))
    c15m = to_candles(t.history(period="30d", interval="15m"))
    daily = to_candles(t.history(period="2y", interval="1d"))
    asset_data[asset] = {"c1m": c1m, "c5m": c5m, "c15m": c15m, "daily": daily}
    print(f"{len(c1m)} 1m, {len(daily)} daily")
    time.sleep(0.5)

# Run all scenarios
print("\n" + "-" * 80)
print("  RUNNING SCENARIOS")
print("-" * 80)

all_results = []
all_signals = []

for asset, time_min, name in SCENARIOS:
    data = asset_data[asset]
    hours = time_min // 60
    mins = time_min % 60

    result = run_scenario(
        asset, data["c1m"], data["c5m"], data["c15m"], data["daily"],
        time_min, name
    )

    if not result:
        print(f"\n  [{hours}:{mins:02d}] {name}")
        print(f"    Insufficient data — skipped")
        continue

    all_results.append(result)

    print(f"\n  [{hours}:{mins:02d}] {name}")
    print(f"    Price: ${result['price']:.2f}  |  Levels: {result['levels']}  |  "
          f"CVD: {result['cvd_bias']}  |  Bias: {result['day_bias']}")
    print(f"    Signals detected: {result['signals_found']}")

    for r in result["results"]:
        icon = "+" if "WIN" in r["outcome"] else "X"
        aligned = "ALIGNED" if r["aligned"] else "COUNTER"
        print(f"    [{icon}] {r['pattern']} {r['direction']} at {r['level']} ${r['level_price']:.2f} "
              f"| RR {r['rr']}:1 {'OK' if r['rr_passes'] else 'LOW'} | {r['outcome']} ${r['pnl']:+.2f} "
              f"| {aligned}")
        all_signals.append(r)

# ═══════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════

print("\n" + "=" * 80)
print("  BACKTEST RESULTS SUMMARY")
print("=" * 80)

total = len(all_signals)
if total == 0:
    print("\n  No signals detected in any scenario.")
    print("  This could mean:")
    print("    - CVD magnitude filter blocked all signals (after hours)")
    print("    - ATR-scaled body filter blocked noise candles")
    print("    - Volume requirements not met")
    print("\n  Try running during market hours for real-time detection.")
else:
    wins = len([s for s in all_signals if s["outcome"] == "WIN"])
    partial_wins = len([s for s in all_signals if s["outcome"] == "PARTIAL_WIN"])
    losses = len([s for s in all_signals if s["outcome"] == "LOSS"])
    partial_losses = len([s for s in all_signals if s["outcome"] == "PARTIAL_LOSS"])

    win_rate = (wins + partial_wins) / total * 100 if total > 0 else 0
    strict_win_rate = wins / total * 100 if total > 0 else 0

    total_pnl = sum(s["pnl"] for s in all_signals)
    avg_win = sum(s["pnl"] for s in all_signals if s["pnl"] > 0) / max(wins + partial_wins, 1)
    avg_loss = sum(s["pnl"] for s in all_signals if s["pnl"] < 0) / max(losses + partial_losses, 1)

    rr_passed = [s for s in all_signals if s["rr_passes"]]
    rr_passed_wins = len([s for s in rr_passed if "WIN" in s["outcome"]])
    rr_passed_rate = rr_passed_wins / len(rr_passed) * 100 if rr_passed else 0

    aligned = [s for s in all_signals if s["aligned"]]
    aligned_wins = len([s for s in aligned if "WIN" in s["outcome"]])
    aligned_rate = aligned_wins / len(aligned) * 100 if aligned else 0

    counter = [s for s in all_signals if not s["aligned"]]
    counter_wins = len([s for s in counter if "WIN" in s["outcome"]])
    counter_rate = counter_wins / len(counter) * 100 if counter else 0

    by_pattern = {}
    for s in all_signals:
        p = s["pattern"]
        by_pattern.setdefault(p, {"total": 0, "wins": 0, "pnl": 0})
        by_pattern[p]["total"] += 1
        if "WIN" in s["outcome"]:
            by_pattern[p]["wins"] += 1
        by_pattern[p]["pnl"] += s["pnl"]

    print(f"\n  TOTAL SIGNALS: {total}")
    print(f"  ├─ Full wins:       {wins}")
    print(f"  ├─ Partial wins:    {partial_wins}")
    print(f"  ├─ Full losses:     {losses}")
    print(f"  └─ Partial losses:  {partial_losses}")
    print(f"\n  WIN RATE:           {win_rate:.0f}% (including partials)")
    print(f"  STRICT WIN RATE:    {strict_win_rate:.0f}% (TP hit only)")
    print(f"  TOTAL P&L:          ${total_pnl:+.2f}")
    print(f"  AVG WIN:            ${avg_win:+.2f}")
    print(f"  AVG LOSS:           ${avg_loss:+.2f}")

    print(f"\n  BY FILTER:")
    print(f"  ├─ RR >= {MIN_RR}:  {len(rr_passed)} signals, {rr_passed_rate:.0f}% win rate")
    print(f"  ├─ Aligned:    {len(aligned)} signals, {aligned_rate:.0f}% win rate")
    print(f"  └─ Counter:    {len(counter)} signals, {counter_rate:.0f}% win rate")

    print(f"\n  BY PATTERN:")
    for p, stats in sorted(by_pattern.items()):
        wr = stats["wins"] / stats["total"] * 100 if stats["total"] > 0 else 0
        print(f"  ├─ {p:15} {stats['total']:>3} signals  {wr:>5.0f}% win  ${stats['pnl']:>+8.2f} P&L")

print("\n" + "=" * 80)
print("  END BACKTEST")
print("=" * 80)
