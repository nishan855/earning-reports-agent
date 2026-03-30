"""
MULTI-DAY BACKTEST — Tests 20 scenarios across different days and assets.
Uses 5-day intraday data from yfinance (5m bars cover 5 trading days).
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
from trading.constants import MIN_RR
from datetime import datetime
import pytz, time

ET = pytz.timezone("America/New_York")

def to_candles(df):
    out = []
    for ts, row in df.iterrows():
        try:
            c = Candle(t=int(ts.timestamp()*1000), o=round(float(row["Open"]),2),
                       h=round(float(row["High"]),2), l=round(float(row["Low"]),2),
                       c=round(float(row["Close"]),2), v=float(row["Volume"]))
            if c.c > 0 and c.h >= c.l: out.append(c)
        except: pass
    return out

def get_day_bars(all_bars, day_idx):
    """Group bars by trading day (9:30-16:00 ET), return day_idx from end (0=most recent)."""
    days = {}
    for c in all_bars:
        et = datetime.fromtimestamp(c.t / 1000, tz=ET)
        day_key = et.strftime("%Y-%m-%d")
        t = et.hour * 60 + et.minute
        if 570 <= t < 960:
            days.setdefault(day_key, []).append(c)
    sorted_days = sorted(days.keys(), reverse=True)
    if day_idx >= len(sorted_days):
        return [], ""
    return days[sorted_days[day_idx]], sorted_days[day_idx]

def filter_before(bars, end_min):
    out = []
    for c in bars:
        et = datetime.fromtimestamp(c.t / 1000, tz=ET)
        if et.hour * 60 + et.minute < end_min:
            out.append(c)
    return out

def filter_after(bars, start_min, end_min):
    out = []
    for c in bars:
        et = datetime.fromtimestamp(c.t / 1000, tz=ET)
        t = et.hour * 60 + et.minute
        if start_min <= t < end_min:
            out.append(c)
    return out

def run_test(asset, day_5m, daily, c15m, scan_time, day_str):
    bars_before = filter_before(day_5m, scan_time)
    bars_after = filter_after(day_5m, scan_time, scan_time + 60)
    if len(bars_before) < 4 or len(bars_after) < 2:
        return None

    price = bars_before[-1].c
    open_p = bars_before[0].o

    or_bars = filter_before(day_5m, 600)
    or_bars = [c for c in or_bars if datetime.fromtimestamp(c.t/1000, tz=ET).hour * 60 + datetime.fromtimestamp(c.t/1000, tz=ET).minute >= 570]
    or_h = max(c.h for c in or_bars) if or_bars else 0
    or_l = min(c.l for c in or_bars) if or_bars else 0

    vwap = calc_vwap(bars_before)
    vp = compute_volume_profile(asset, bars_before) if len(bars_before) >= 5 else None
    zones = detect_zones(daily, price)

    levels = build_levels(asset, daily, bars_before, bars_before, price, vwap,
                          or_h, or_l, scan_time >= 600, vp, zones)
    if not levels:
        return None

    atr_bars = bars_before[-14:]
    atr = sum(c.h - c.l for c in atr_bars) / len(atr_bars) if atr_bars else 1.0
    avg_vol = sum(c.v for c in bars_before[-20:]) / max(len(bars_before[-20:]), 1)

    # CVD approximation
    cvd = 0.0
    total_vol = sum(c.v for c in bars_before)
    for c in bars_before:
        rng = c.h - c.l
        if rng > 0:
            ratio = (c.c - c.l) / rng
            cvd += c.v * (ratio - 0.5) * 2
    cvd_bias = "BUY" if cvd > total_vol * 0.005 else "SELL" if cvd < -total_vol * 0.005 else "NEUT"

    # Scan patterns
    signals = []
    if len(bars_before) >= 2:
        candle = bars_before[-1]
        prev = bars_before[-2]
        cvd_ch = candle.v * 0.6 * (1 if candle.c > candle.o else -1)
        cvd_sc = cvd_ch / 1000
        avg_sc = avg_vol / 1000

        for level in levels:
            if level.score < 6: continue

            is_bo, bo_dir = detect_breakout(candle, prev, level, avg_sc, 0, cvd_sc, atr)
            if is_bo:
                signals.append(("BREAKOUT", bo_dir, level))

            is_rej, rej_dir, strength = detect_rejection(candle, level, avg_sc, 0, cvd_sc, atr)
            if is_rej:
                signals.append(("REJECTION", rej_dir, level))

            sh_candle = bars_before[-1]
            is_sh, sh_dir = detect_stop_hunt(sh_candle, level, avg_sc, cvd_sc, atr)
            if is_sh:
                signals.append(("STOP_HUNT", sh_dir, level))

    # Outcome
    future_high = max(c.h for c in bars_after)
    future_low = min(c.l for c in bars_after)
    future_close = bars_after[-1].c

    results = []
    for pattern, direction, level in signals:
        entry = price
        if direction == "BULLISH":
            stop = level.price - atr * 0.5
            targets = sorted([l for l in levels if l.price > price], key=lambda l: l.price)
            target = targets[0].price if targets else price + atr * 3
        else:
            stop = level.price + atr * 0.5
            targets = sorted([l for l in levels if l.price < price], key=lambda l: l.price, reverse=True)
            target = targets[0].price if targets else price - atr * 3

        risk = abs(entry - stop)
        reward = abs(target - entry)
        rr = reward / risk if risk > 0 else 0

        if direction == "BULLISH":
            hit_tp = future_high >= target
            hit_sl = future_low <= stop
        else:
            hit_tp = future_low <= target
            hit_sl = future_high >= stop

        if hit_tp and not hit_sl:
            outcome = "WIN"
            pnl = reward
        elif hit_sl:
            outcome = "LOSS"
            pnl = -risk
        elif (direction == "BULLISH" and future_close > entry) or (direction == "BEARISH" and future_close < entry):
            outcome = "P_WIN"
            pnl = abs(future_close - entry)
        else:
            outcome = "P_LOSS"
            pnl = -abs(future_close - entry)

        day_bias_match = (cvd_bias == "BUY" and direction == "BULLISH") or (cvd_bias == "SELL" and direction == "BEARISH")

        results.append({
            "day": day_str, "asset": asset, "time": scan_time,
            "pattern": pattern, "dir": direction,
            "level": level.name, "lprice": level.price, "score": level.score,
            "entry": entry, "stop": round(stop, 2), "target": round(target, 2),
            "rr": round(rr, 1), "rr_ok": rr >= MIN_RR,
            "outcome": outcome, "pnl": round(pnl, 2),
            "cvd": cvd_bias, "aligned": day_bias_match,
            "f_high": future_high, "f_low": future_low, "f_close": future_close,
        })

    return results


# ═══════════════════════════════════════════════════════════
print("=" * 90)
print("  MULTI-DAY BACKTEST — 20 SCENARIOS ACROSS 5 TRADING DAYS")
print("=" * 90)

ASSETS = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA", "MSFT"]
TIMES = [630, 660, 720, 780, 870]  # 10:30, 11:00, 12:00, 1:00, 2:30

print("\nFetching 5-day 5m + 2yr daily data...")
data = {}
for asset in ASSETS:
    print(f"  {asset}...", end=" ", flush=True)
    t = yf.Ticker(asset)
    c5m = to_candles(t.history(period="5d", interval="5m"))
    c15m = to_candles(t.history(period="30d", interval="15m"))
    daily = to_candles(t.history(period="2y", interval="1d"))
    data[asset] = {"c5m": c5m, "c15m": c15m, "daily": daily}
    # Count trading days
    days = set()
    for c in c5m:
        et = datetime.fromtimestamp(c.t/1000, tz=ET)
        if 570 <= et.hour*60+et.minute < 960:
            days.add(et.strftime("%Y-%m-%d"))
    print(f"{len(c5m)} 5m bars, {len(days)} trading days")
    time.sleep(0.5)

# Build 20 scenarios
all_signals = []
scenario_count = 0

print("\n" + "-" * 90)

for asset in ASSETS:
    d = data[asset]
    for day_idx in range(5):  # 5 most recent trading days
        day_bars, day_str = get_day_bars(d["c5m"], day_idx)
        if not day_bars or not day_str:
            continue

        for scan_time in TIMES:
            if scenario_count >= 20:
                break

            results = run_test(asset, day_bars, d["daily"], d["c15m"], scan_time, day_str)
            if results is None:
                continue

            scenario_count += 1
            h = scan_time // 60
            m = scan_time % 60

            if results:
                for r in results:
                    all_signals.append(r)
                    icon = "+" if "WIN" in r["outcome"] else "X"
                    al = "AL" if r["aligned"] else "CT"
                    rr_tag = "OK" if r["rr_ok"] else "LOW"
                    print(f"  #{scenario_count:>2}  {r['day']} {h}:{m:02d}  {r['asset']:5} "
                          f"{r['pattern']:10} {r['dir']:7} at {r['level']:8} ${r['lprice']:>8.2f} "
                          f"s={r['score']:>2} RR={r['rr']:>4.1f} {rr_tag:>3} "
                          f"[{icon}] {r['outcome']:>6} ${r['pnl']:>+7.2f}  {al}")
            else:
                print(f"  #{scenario_count:>2}  {day_str} {h}:{m:02d}  {asset:5}  -- no signals --")

            if scenario_count >= 20:
                break
        if scenario_count >= 20:
            break
    if scenario_count >= 20:
        break

# ═══════════════════════════════════════════════════════════
print("\n" + "=" * 90)
print("  RESULTS TABLE")
print("=" * 90)
print(f"  {'#':>2}  {'Date':10} {'Time':>5}  {'Asset':5} {'Pattern':10} {'Dir':7} "
      f"{'Level':8} {'Score':>5} {'RR':>4} {'Pass':>4} {'Result':>6} {'P&L':>8} {'Algn':>4}")
print("-" * 90)

for i, s in enumerate(all_signals):
    h = s["time"] // 60
    m = s["time"] % 60
    icon = "+" if "WIN" in s["outcome"] else "X"
    print(f"  {i+1:>2}  {s['day']:10} {h}:{m:02d}  {s['asset']:5} {s['pattern']:10} {s['dir']:7} "
          f"{s['level']:8} {s['score']:>5} {s['rr']:>4.1f} {'Y' if s['rr_ok'] else 'N':>4} "
          f"{s['outcome']:>6} ${s['pnl']:>+7.2f} {'Y' if s['aligned'] else 'N':>4}")

# ═══════════════════════════════════════════════════════════
print("\n" + "=" * 90)
print("  PERFORMANCE SUMMARY")
print("=" * 90)

total = len(all_signals)
if total == 0:
    print("  No signals detected.")
else:
    wins = len([s for s in all_signals if s["outcome"] == "WIN"])
    p_wins = len([s for s in all_signals if s["outcome"] == "P_WIN"])
    losses = len([s for s in all_signals if s["outcome"] == "LOSS"])
    p_losses = len([s for s in all_signals if s["outcome"] == "P_LOSS"])

    total_pnl = sum(s["pnl"] for s in all_signals)
    win_rate = (wins + p_wins) / total * 100
    strict_wr = wins / total * 100

    rr_ok = [s for s in all_signals if s["rr_ok"]]
    rr_ok_wins = len([s for s in rr_ok if "WIN" in s["outcome"]])
    rr_ok_wr = rr_ok_wins / len(rr_ok) * 100 if rr_ok else 0
    rr_ok_pnl = sum(s["pnl"] for s in rr_ok)

    rr_low = [s for s in all_signals if not s["rr_ok"]]
    rr_low_wins = len([s for s in rr_low if "WIN" in s["outcome"]])
    rr_low_pnl = sum(s["pnl"] for s in rr_low)

    aligned = [s for s in all_signals if s["aligned"]]
    al_wins = len([s for s in aligned if "WIN" in s["outcome"]])
    al_wr = al_wins / len(aligned) * 100 if aligned else 0

    counter = [s for s in all_signals if not s["aligned"]]
    ct_wins = len([s for s in counter if "WIN" in s["outcome"]])
    ct_wr = ct_wins / len(counter) * 100 if counter else 0

    by_pattern = {}
    for s in all_signals:
        p = s["pattern"]
        by_pattern.setdefault(p, {"n": 0, "w": 0, "pnl": 0})
        by_pattern[p]["n"] += 1
        if "WIN" in s["outcome"]: by_pattern[p]["w"] += 1
        by_pattern[p]["pnl"] += s["pnl"]

    by_asset = {}
    for s in all_signals:
        a = s["asset"]
        by_asset.setdefault(a, {"n": 0, "w": 0, "pnl": 0})
        by_asset[a]["n"] += 1
        if "WIN" in s["outcome"]: by_asset[a]["w"] += 1
        by_asset[a]["pnl"] += s["pnl"]

    avg_win = sum(s["pnl"] for s in all_signals if s["pnl"] > 0) / max(wins + p_wins, 1)
    avg_loss = sum(s["pnl"] for s in all_signals if s["pnl"] < 0) / max(losses + p_losses, 1)

    print(f"""
  OVERALL
    Total signals:     {total}
    Wins:              {wins} full + {p_wins} partial = {wins+p_wins}
    Losses:            {losses} full + {p_losses} partial = {losses+p_losses}
    Win rate:          {win_rate:.0f}% (all)  |  {strict_wr:.0f}% (strict TP hit)
    Total P&L:         ${total_pnl:+.2f}
    Avg win:           ${avg_win:+.2f}
    Avg loss:          ${avg_loss:+.2f}

  RR FILTER IMPACT
    RR >= {MIN_RR}:      {len(rr_ok)} signals, {rr_ok_wr:.0f}% win rate, ${rr_ok_pnl:+.2f} P&L
    RR < {MIN_RR}:       {len(rr_low)} signals, ${rr_low_pnl:+.2f} P&L  ← WOULD BE BLOCKED

  ALIGNMENT IMPACT
    Aligned (CVD+dir):  {len(aligned)} signals, {al_wr:.0f}% win rate
    Counter-trend:      {len(counter)} signals, {ct_wr:.0f}% win rate

  BY PATTERN""")
    for p, st in sorted(by_pattern.items()):
        wr = st["w"] / st["n"] * 100 if st["n"] > 0 else 0
        print(f"    {p:15} {st['n']:>3} signals  {wr:>5.0f}% win  ${st['pnl']:>+8.2f}")

    print(f"\n  BY ASSET")
    for a, st in sorted(by_asset.items()):
        wr = st["w"] / st["n"] * 100 if st["n"] > 0 else 0
        print(f"    {a:5} {st['n']:>3} signals  {wr:>5.0f}% win  ${st['pnl']:>+8.2f}")

    # Filtered results (what system would actually trade)
    filtered = [s for s in all_signals if s["rr_ok"] and s["aligned"]]
    if filtered:
        f_wins = len([s for s in filtered if "WIN" in s["outcome"]])
        f_pnl = sum(s["pnl"] for s in filtered)
        f_wr = f_wins / len(filtered) * 100
        print(f"""
  FILTERED (RR >= {MIN_RR} AND aligned) — WHAT SYSTEM WOULD ACTUALLY TRADE
    Signals:  {len(filtered)}
    Win rate: {f_wr:.0f}%
    P&L:      ${f_pnl:+.2f}""")
    else:
        print(f"\n  FILTERED: 0 signals would pass all gates")

print("\n" + "=" * 90)
