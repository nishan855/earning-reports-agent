"""
LARGE SCALE BACKTEST — 500 scenarios across all assets and all available days.
Uses 5m bars (5 days) and daily bars (2 years) from yfinance.
Tests detection engine only (no LLM calls).
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
from trading.constants import MIN_RR
from trading.context.day_context import assess_day_context
from datetime import datetime
import pytz, time, json

ET = pytz.timezone("America/New_York")
ASSETS = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA", "MSFT", "META", "AMZN"]
SCAN_TIMES = [615, 630, 645, 660, 690, 720, 750, 780, 810, 840, 870, 900, 930]


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


def get_trading_days(bars):
    days = {}
    for c in bars:
        et = datetime.fromtimestamp(c.t / 1000, tz=ET)
        t = et.hour * 60 + et.minute
        if 570 <= t < 960:
            day = et.strftime("%Y-%m-%d")
            days.setdefault(day, []).append(c)
    return days


def filter_before(bars, end_min):
    return [c for c in bars if datetime.fromtimestamp(c.t/1000, tz=ET).hour * 60 + datetime.fromtimestamp(c.t/1000, tz=ET).minute < end_min]


def filter_range(bars, start_min, end_min):
    return [c for c in bars if start_min <= datetime.fromtimestamp(c.t/1000, tz=ET).hour * 60 + datetime.fromtimestamp(c.t/1000, tz=ET).minute < end_min]


def run_scan(asset, day_bars, daily, scan_time):
    before = filter_before(day_bars, scan_time)
    after = filter_range(day_bars, scan_time, min(scan_time + 60, 960))
    if len(before) < 4 or len(after) < 2:
        return []

    price = before[-1].c
    or_bars = filter_range(day_bars, 570, 600)
    or_h = max(c.h for c in or_bars) if or_bars else 0
    or_l = min(c.l for c in or_bars) if or_bars else 0
    vwap = calc_vwap(before)
    vp = compute_volume_profile(asset, before) if len(before) >= 5 else None
    zones = detect_zones(daily, price)

    levels = build_levels(asset, daily, before, before, price, vwap, or_h, or_l, scan_time >= 600, vp, zones)
    if not levels:
        return []

    atr = sum(c.h - c.l for c in before[-14:]) / min(len(before[-14:]), 14) if before else 1.0
    avg_vol = sum(c.v for c in before[-20:]) / max(len(before[-20:]), 1)

    # CVD approx
    cvd = sum(c.v * ((c.c - c.l) / max(c.h - c.l, 0.01) - 0.5) * 2 for c in before)
    total_vol = sum(c.v for c in before)
    cvd_bias = "BUY" if cvd > total_vol * 0.005 else "SELL" if cvd < -total_vol * 0.005 else "NEUT"

    signals = []
    if len(before) >= 2:
        candle, prev = before[-1], before[-2]
        cvd_sc = candle.v * 0.6 * (1 if candle.c > candle.o else -1) / 1000
        avg_sc = avg_vol / 1000

        for level in levels:
            if level.score < 6: continue

            for detect_fn, pat_name in [(lambda c,p,l: detect_breakout(c,p,l,avg_sc,0,cvd_sc,atr), "BO"),
                                         (lambda c,p,l: (detect_rejection(c,l,avg_sc,0,cvd_sc,atr)[0], detect_rejection(c,l,avg_sc,0,cvd_sc,atr)[1]), "REJ"),
                                         (lambda c,p,l: detect_stop_hunt(c,l,avg_sc,cvd_sc,atr), "SH")]:
                try:
                    result = detect_fn(candle, prev, level)
                    is_det, direction = result[0], result[1]
                except:
                    continue

                if not is_det or not direction:
                    continue

                entry = price
                if direction == "BULLISH":
                    stop = level.price - atr * 0.5
                    tgts = sorted([l for l in levels if l.price > price], key=lambda l: l.price)
                    target = tgts[0].price if tgts else price + atr * 3
                else:
                    stop = level.price + atr * 0.5
                    tgts = sorted([l for l in levels if l.price < price], key=lambda l: l.price, reverse=True)
                    target = tgts[0].price if tgts else price - atr * 3

                risk = abs(entry - stop)
                reward = abs(target - entry)
                rr = reward / risk if risk > 0 else 0

                f_high = max(c.h for c in after)
                f_low = min(c.l for c in after)
                f_close = after[-1].c

                if direction == "BULLISH":
                    hit_tp = f_high >= target
                    hit_sl = f_low <= stop
                else:
                    hit_tp = f_low <= target
                    hit_sl = f_high >= stop

                if hit_tp and not hit_sl: outcome, pnl = "WIN", reward
                elif hit_sl: outcome, pnl = "LOSS", -risk
                elif (direction == "BULLISH" and f_close > entry) or (direction == "BEARISH" and f_close < entry):
                    outcome, pnl = "P_WIN", abs(f_close - entry)
                else: outcome, pnl = "P_LOSS", -abs(f_close - entry)

                aligned = (cvd_bias == "BUY" and direction == "BULLISH") or (cvd_bias == "SELL" and direction == "BEARISH")

                signals.append({
                    "asset": asset, "pattern": pat_name, "dir": direction,
                    "level": level.name, "score": level.score,
                    "rr": round(rr, 1), "rr_ok": rr >= MIN_RR,
                    "outcome": outcome, "pnl": round(pnl, 2),
                    "aligned": aligned, "cvd": cvd_bias,
                    "time": scan_time,
                })
                break  # one signal per level per scan
    return signals


# ═══════════════════════════════════════════════════════════
print("=" * 80)
print("  500-SCENARIO BACKTEST")
print("=" * 80)

print("\nFetching data for 8 assets...")
asset_data = {}
for asset in ASSETS:
    print(f"  {asset}...", end=" ", flush=True)
    t = yf.Ticker(asset)
    c5m = to_candles(t.history(period="5d", interval="5m"))
    daily = to_candles(t.history(period="2y", interval="1d"))
    days = get_trading_days(c5m)
    asset_data[asset] = {"days": days, "daily": daily}
    print(f"{len(c5m)} bars, {len(days)} days")
    time.sleep(0.5)

# Run scenarios
all_signals = []
scenario_count = 0

print(f"\nRunning scans ({len(ASSETS)} assets x {len(SCAN_TIMES)} times x 5 days = {len(ASSETS)*len(SCAN_TIMES)*5} max)...")

for asset in ASSETS:
    d = asset_data[asset]
    for day_str, day_bars in sorted(d["days"].items()):
        for scan_time in SCAN_TIMES:
            scenario_count += 1
            results = run_scan(asset, day_bars, d["daily"], scan_time)
            all_signals.extend(results)

print(f"  Scanned: {scenario_count} scenarios")
print(f"  Signals found: {len(all_signals)}")

# ═══════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("  FULL RESULTS TABLE")
print("=" * 80)
print(f"  {'#':>3} {'Asset':5} {'Pat':3} {'Dir':4} {'Level':8} {'Sc':>2} {'RR':>4} {'OK':>2} {'Out':>5} {'P&L':>7} {'Al':>2} {'CVD':>4}")
print("-" * 80)

for i, s in enumerate(all_signals[:100]):  # show first 100
    print(f"  {i+1:>3} {s['asset']:5} {s['pattern']:3} {s['dir'][:4]:4} {s['level']:8} {s['score']:>2} "
          f"{s['rr']:>4.1f} {'Y' if s['rr_ok'] else 'N':>2} {s['outcome']:>5} ${s['pnl']:>+6.2f} "
          f"{'Y' if s['aligned'] else 'N':>2} {s['cvd']:>4}")

if len(all_signals) > 100:
    print(f"  ... +{len(all_signals)-100} more signals")

# ═══════════════════════════════════════════════════════════
print("\n" + "=" * 80)
print("  PERFORMANCE ANALYSIS")
print("=" * 80)

total = len(all_signals)
if total == 0:
    print("  No signals.")
else:
    wins = len([s for s in all_signals if s["outcome"] == "WIN"])
    p_wins = len([s for s in all_signals if s["outcome"] == "P_WIN"])
    losses = len([s for s in all_signals if s["outcome"] == "LOSS"])
    p_losses = len([s for s in all_signals if s["outcome"] == "P_LOSS"])
    total_pnl = sum(s["pnl"] for s in all_signals)
    avg_win = sum(s["pnl"] for s in all_signals if s["pnl"] > 0) / max(wins + p_wins, 1)
    avg_loss = sum(s["pnl"] for s in all_signals if s["pnl"] < 0) / max(losses + p_losses, 1)

    # Filtered
    filt_rr = [s for s in all_signals if s["rr_ok"]]
    filt_al = [s for s in all_signals if s["aligned"]]
    filt_both = [s for s in all_signals if s["rr_ok"] and s["aligned"]]

    def stats(label, sigs):
        if not sigs: return
        w = len([s for s in sigs if "WIN" in s["outcome"]])
        l = len([s for s in sigs if "LOSS" in s["outcome"]])
        wr = w / len(sigs) * 100
        pnl = sum(s["pnl"] for s in sigs)
        aw = sum(s["pnl"] for s in sigs if s["pnl"] > 0) / max(w, 1)
        al = sum(s["pnl"] for s in sigs if s["pnl"] < 0) / max(l, 1)
        pf = abs(sum(s["pnl"] for s in sigs if s["pnl"] > 0)) / max(abs(sum(s["pnl"] for s in sigs if s["pnl"] < 0)), 0.01)
        print(f"    {label:30} {len(sigs):>4} sigs  {wr:>5.1f}% win  ${pnl:>+8.2f} P&L  "
              f"avg W ${aw:>+5.2f}  avg L ${al:>+5.2f}  PF {pf:.2f}")

    print(f"\n  TOTAL SIGNALS: {total}")
    print(f"  Wins: {wins}  Partial wins: {p_wins}  Losses: {losses}  Partial losses: {p_losses}")
    print(f"  Total P&L: ${total_pnl:+.2f}  |  Avg win: ${avg_win:+.2f}  |  Avg loss: ${avg_loss:+.2f}")

    print(f"\n  FILTER COMPARISON:")
    stats("ALL (no filter)", all_signals)
    stats(f"RR >= {MIN_RR} only", filt_rr)
    stats("Aligned only", filt_al)
    stats(f"RR >= {MIN_RR} + Aligned", filt_both)

    # By pattern
    print(f"\n  BY PATTERN:")
    for pat in sorted(set(s["pattern"] for s in all_signals)):
        stats(f"  {pat}", [s for s in all_signals if s["pattern"] == pat])
        # Filtered version
        f = [s for s in filt_both if s["pattern"] == pat]
        if f:
            stats(f"  {pat} (filtered)", f)

    # By asset
    print(f"\n  BY ASSET:")
    for asset in sorted(set(s["asset"] for s in all_signals)):
        stats(f"  {asset}", [s for s in all_signals if s["asset"] == asset])

    # By asset filtered
    print(f"\n  BY ASSET (filtered RR+aligned):")
    for asset in sorted(set(s["asset"] for s in filt_both)):
        stats(f"  {asset}", [s for s in filt_both if s["asset"] == asset])

    # By time of day
    print(f"\n  BY TIME OF DAY:")
    for t in sorted(set(s["time"] for s in all_signals)):
        h, m = t // 60, t % 60
        stats(f"  {h}:{m:02d}", [s for s in all_signals if s["time"] == t])

    # By score
    print(f"\n  BY LEVEL SCORE:")
    for sc in sorted(set(s["score"] for s in all_signals)):
        stats(f"  Score {sc}", [s for s in all_signals if s["score"] == sc])

    # Win rate by RR bucket
    print(f"\n  BY RR BUCKET:")
    for lo, hi, label in [(0, 1, "0-1"), (1, 2, "1-2"), (2, 3, "2-3"), (3, 5, "3-5"), (5, 100, "5+")]:
        bucket = [s for s in all_signals if lo <= s["rr"] < hi]
        if bucket:
            stats(f"  RR {label}", bucket)

print("\n" + "=" * 80)
print("  END BACKTEST")
print("=" * 80)
