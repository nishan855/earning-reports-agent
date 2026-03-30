"""Detection Engine Backtest v2 — Accurate methodology.

Fixes from v1:
1. Uses 5m ATR (not 1m) for stop placement
2. CVD processed incrementally (no look-ahead bias)
3. Gate simulation (cooldowns, locks, budget)
4. Stop = level ± ATR_5m × 0.3 (structural, not noise-level)
5. Outcome window = rest of day until 3:30 PM (not arbitrary 60 bars)
6. Forced close at 3:30 PM for any open position
7. Dedup: one signal per level per day, 5-bar cooldown between signals
"""

import sys
import pytz
import yfinance as yf
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, ".")

from trading.models import Candle, Level, DayContext
from trading.constants import ASSETS, MIN_LEVEL_SCORE
from trading.detection.breakout import detect_breakout
from trading.detection.rejection import detect_rejection
from trading.detection.stop_hunt import detect_stop_hunt
from trading.levels.builder import build_levels, calc_vwap
from trading.levels.volume_profile import compute_volume_profile
from trading.levels.zones import detect_zones
from trading.context.day_context import assess_day_context

ET = pytz.timezone("America/New_York")
CUTOFF_MIN = 930  # 3:30 PM = no new signals, force close open positions


def load_data(asset):
    t = yf.Ticker(asset)
    result = {}
    for tf, (p, i) in {"1m": ("5d", "1m"), "5m": ("5d", "5m"), "1d": ("2y", "1d")}.items():
        df = t.history(period=p, interval=i)
        bars = []
        for ts, row in df.iterrows():
            try:
                c = Candle(t=int(ts.timestamp() * 1000), o=round(float(row["Open"]), 2),
                           h=round(float(row["High"]), 2), l=round(float(row["Low"]), 2),
                           c=round(float(row["Close"]), 2), v=float(row["Volume"]))
                if c.c > 0 and c.h >= c.l:
                    bars.append(c)
            except Exception:
                pass
        result[tf] = bars
    return result


def get_market_bars(bars, target_date):
    return [c for c in bars
            if datetime.fromtimestamp(c.t / 1000, tz=ET).strftime("%Y-%m-%d") == target_date
            and 570 <= datetime.fromtimestamp(c.t / 1000, tz=ET).hour * 60 +
                       datetime.fromtimestamp(c.t / 1000, tz=ET).minute < 960]


def calc_atr_5m(bars_5m, period=14):
    """ATR from 5m bars — appropriate scale for stop placement."""
    if len(bars_5m) < 2:
        return 1.0
    trs = []
    for i in range(1, min(period + 1, len(bars_5m))):
        tr = max(bars_5m[i].h - bars_5m[i].l,
                 abs(bars_5m[i].h - bars_5m[i - 1].c),
                 abs(bars_5m[i].l - bars_5m[i - 1].c))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 1.0


def build_5m_from_1m(bars_1m):
    result = []
    for i in range(0, len(bars_1m) - 4, 5):
        chunk = bars_1m[i:i + 5]
        if len(chunk) < 3:
            continue
        result.append(Candle(
            t=chunk[0].t, o=chunk[0].o,
            h=max(c.h for c in chunk), l=min(c.l for c in chunk),
            c=chunk[-1].c, v=sum(c.v for c in chunk),
        ))
    return result


class IncrementalCVD:
    """Process CVD bar-by-bar without look-ahead bias."""
    def __init__(self):
        self.value = 0.0
        self.prev_value = 0.0
        self._last_price = None
        self._total_vol = 0.0

    def process_bar(self, bar):
        """Process one 1m bar: 4 synthetic ticks (open, low/high, close)."""
        self.prev_value = self.value
        ticks = [bar.o]
        if bar.c >= bar.o:
            ticks += [bar.l, bar.h, bar.c]
        else:
            ticks += [bar.h, bar.l, bar.c]
        vol_chunk = bar.v / 4
        for price in ticks:
            if self._last_price is not None:
                if price > self._last_price:
                    self.value += vol_chunk
                elif price < self._last_price:
                    self.value -= vol_chunk
            self._total_vol += vol_chunk
            self._last_price = price

    @property
    def change(self):
        return self.value - self.prev_value

    @property
    def bias(self):
        threshold = max(self._total_vol * 0.005, 500)
        if self.value > threshold:
            return "BUYERS"
        elif self.value < -threshold:
            return "SELLERS"
        return "NEUTRAL"


def evaluate_outcome(direction, entry, stop, tp1, tp2, future_bars, cutoff_min=CUTOFF_MIN):
    """Walk forward bar-by-bar. Check stop/TP hit. Force close at 3:30 PM."""
    for bar in future_bars:
        dt = datetime.fromtimestamp(bar.t / 1000, tz=ET)
        t_min = dt.hour * 60 + dt.minute

        # Force close at 3:30 PM
        if t_min >= cutoff_min:
            pnl = (bar.c - entry) if direction == "BULLISH" else (entry - bar.c)
            risk = abs(entry - stop)
            r_mult = pnl / risk if risk > 0 else 0
            if pnl > 0:
                return "WIN_TIME", bar.c, round(r_mult, 2)
            else:
                return "LOSS_TIME", bar.c, round(r_mult, 2)

        if direction == "BULLISH":
            # Check stop first (conservative — assumes worst case within bar)
            if bar.l <= stop:
                return "LOSS", stop, -1.0
            if bar.h >= tp1:
                risk = abs(entry - stop)
                r_mult = abs(tp1 - entry) / risk if risk > 0 else 0
                return "WIN_TP1", tp1, round(r_mult, 2)
            if tp2 > 0 and bar.h >= tp2:
                risk = abs(entry - stop)
                r_mult = abs(tp2 - entry) / risk if risk > 0 else 0
                return "WIN_TP2", tp2, round(r_mult, 2)
        elif direction == "BEARISH":
            if bar.h >= stop:
                return "LOSS", stop, -1.0
            if bar.l <= tp1:
                risk = abs(entry - stop)
                r_mult = abs(entry - tp1) / risk if risk > 0 else 0
                return "WIN_TP1", tp1, round(r_mult, 2)
            if tp2 > 0 and bar.l <= tp2:
                risk = abs(entry - stop)
                r_mult = abs(entry - tp2) / risk if risk > 0 else 0
                return "WIN_TP2", tp2, round(r_mult, 2)

    return "EXPIRED", entry, 0.0


def run_backtest():
    print("=" * 70)
    print("  DETECTION ENGINE BACKTEST v2 (Accurate Methodology)")
    print("  5m ATR stops | Incremental CVD | Gate sim | Full-day outcomes")
    print("=" * 70)
    print()

    all_data = {}
    for asset in ASSETS:
        print(f"  Loading {asset}...", end=" ", flush=True)
        all_data[asset] = load_data(asset)
        n1m = len(all_data[asset]["1m"])
        nd = len(all_data[asset]["1d"])
        print(f"{n1m} 1m, {nd} daily")

    # Get dates
    spy_1m = all_data["SPY"]["1m"]
    dates = sorted(set(
        datetime.fromtimestamp(c.t / 1000, tz=ET).strftime("%Y-%m-%d")
        for c in spy_1m
        if 570 <= datetime.fromtimestamp(c.t / 1000, tz=ET).hour * 60 +
                  datetime.fromtimestamp(c.t / 1000, tz=ET).minute < 960
    ))
    print(f"\n  Days: {', '.join(dates)}\n")

    all_signals = []

    for date in dates:
        print(f"  --- {date} ---")

        for asset in ASSETS:
            data = all_data[asset]
            daily = data["1d"]
            day_1m = get_market_bars(data["1m"], date)
            day_5m_raw = get_market_bars(data["5m"], date)

            if len(day_1m) < 60 or len(daily) < 22:
                continue

            # Zones from daily (pre-computed, no look-ahead)
            # Use daily bars up to the day BEFORE the test date
            daily_before = [b for b in daily
                            if datetime.fromtimestamp(b.t / 1000, tz=ET).strftime("%Y-%m-%d") < date]
            zones = detect_zones(daily_before, day_1m[0].o) if len(daily_before) >= 20 else []

            # Opening Range
            or_bars = day_1m[:30]
            or_high = max(c.h for c in or_bars)
            or_low = min(c.l for c in or_bars)

            # Day context (use daily bars before this day only)
            dc = assess_day_context(asset, daily_before, [], day_1m[:30], or_high, or_low, day_1m[30].c if len(day_1m) > 30 else day_1m[-1].c)

            # Incremental CVD — process bar by bar
            cvd = IncrementalCVD()
            for c in day_1m[:30]:
                cvd.process_bar(c)

            # Gate simulation
            last_signal_bar = -999
            fired_levels = set()
            signals_today = 0
            MAX_SIGNALS = 6

            # Scan from bar 30 (after OR) to end
            for i in range(30, len(day_1m)):
                bar = day_1m[i]
                dt = datetime.fromtimestamp(bar.t / 1000, tz=ET)
                t_min = dt.hour * 60 + dt.minute

                # Signal hours only
                if t_min < 600 or t_min >= 930:
                    cvd.process_bar(bar)
                    continue

                # Process CVD for this bar FIRST (no look-ahead)
                cvd.process_bar(bar)

                # Budget check
                if signals_today >= MAX_SIGNALS:
                    continue

                # Cooldown: 5 bars between signals
                if i - last_signal_bar < 5:
                    continue

                # Build context up to this bar only
                bars_so_far = day_1m[:i + 1]
                bars_5m_so_far = build_5m_from_1m(bars_so_far)
                atr_5m = calc_atr_5m(bars_5m_so_far)

                if atr_5m <= 0.01:
                    continue

                today_bars = bars_so_far
                vwap = calc_vwap(today_bars)
                vp = compute_volume_profile(asset, today_bars, atr_5m) if len(today_bars) >= 10 else None

                levels = build_levels(
                    asset, daily_before, today_bars, bars_5m_so_far, bar.c, vwap,
                    or_high, or_low, True, vp, zones, gap_pct=dc.gap_pct,
                )

                future_bars = day_1m[i + 1:]  # rest of day
                avg_vol_1m = sum(c.v for c in bars_so_far[-20:]) / min(len(bars_so_far), 20)
                avg_vol_5m = sum(c.v for c in bars_5m_so_far[-20:]) / max(1, min(len(bars_5m_so_far), 20)) if bars_5m_so_far else avg_vol_1m * 5

                detected = False

                # ── STOP HUNT (1m) ──
                for level in levels:
                    if level.score < MIN_LEVEL_SCORE or level.name in fired_levels:
                        continue
                    is_hunt, hunt_dir = detect_stop_hunt(bar, level, avg_vol_1m, cvd.change, atr_5m)
                    if is_hunt:
                        # Structural stop: level ± ATR_5m × 0.3
                        if hunt_dir == "BULLISH":
                            entry = bar.c
                            stop = level.price - atr_5m * 0.3
                            tp_levels = sorted([l for l in levels if l.price > entry and l.price != level.price], key=lambda l: l.price)
                        else:
                            entry = bar.c
                            stop = level.price + atr_5m * 0.3
                            tp_levels = sorted([l for l in levels if l.price < entry and l.price != level.price], key=lambda l: l.price, reverse=True)

                        tp1 = tp_levels[0].price if tp_levels else (entry + atr_5m * 2 if hunt_dir == "BULLISH" else entry - atr_5m * 2)
                        tp2 = tp_levels[1].price if len(tp_levels) > 1 else 0

                        risk = abs(entry - stop)
                        reward = abs(tp1 - entry)
                        rr = reward / risk if risk > 0 else 0
                        if rr < 2.0:
                            continue

                        outcome, exit_price, r_mult = evaluate_outcome(hunt_dir, entry, stop, tp1, tp2, future_bars)
                        all_signals.append({
                            "date": date, "time": dt.strftime("%H:%M"), "asset": asset,
                            "pattern": "STOP_HUNT", "direction": hunt_dir,
                            "level": level.name, "score": level.score,
                            "entry": round(entry, 2), "stop": round(stop, 2),
                            "tp1": round(tp1, 2), "rr": round(rr, 1),
                            "outcome": outcome, "r_mult": r_mult,
                            "atr_5m": round(atr_5m, 3),
                        })
                        fired_levels.add(level.name)
                        last_signal_bar = i
                        signals_today += 1
                        detected = True
                        break

                if detected:
                    continue

                # ── BREAKOUT + REJECTION (5m boundaries) ──
                if (dt.minute + 1) % 5 != 0 or len(bars_5m_so_far) < 2:
                    continue

                last_5m = bars_5m_so_far[-1]
                prev_5m = bars_5m_so_far[-2]

                for level in levels:
                    if level.score < MIN_LEVEL_SCORE or level.name in fired_levels:
                        continue

                    # Breakout
                    is_bo, bo_dir = detect_breakout(last_5m, prev_5m, level, avg_vol_5m, cvd.prev_value, cvd.value, atr_5m)
                    if is_bo:
                        if bo_dir == "BULLISH":
                            entry = last_5m.c
                            stop = level.price - atr_5m * 0.3
                            tp_levels = sorted([l for l in levels if l.price > entry and l.price != level.price], key=lambda l: l.price)
                        else:
                            entry = last_5m.c
                            stop = level.price + atr_5m * 0.3
                            tp_levels = sorted([l for l in levels if l.price < entry and l.price != level.price], key=lambda l: l.price, reverse=True)

                        tp1 = tp_levels[0].price if tp_levels else (entry + atr_5m * 2 if bo_dir == "BULLISH" else entry - atr_5m * 2)
                        tp2 = tp_levels[1].price if len(tp_levels) > 1 else 0
                        risk = abs(entry - stop)
                        reward = abs(tp1 - entry)
                        rr = reward / risk if risk > 0 else 0
                        if rr < 2.0:
                            continue

                        outcome, exit_price, r_mult = evaluate_outcome(bo_dir, entry, stop, tp1, tp2, future_bars)
                        all_signals.append({
                            "date": date, "time": dt.strftime("%H:%M"), "asset": asset,
                            "pattern": "BREAKOUT", "direction": bo_dir,
                            "level": level.name, "score": level.score,
                            "entry": round(entry, 2), "stop": round(stop, 2),
                            "tp1": round(tp1, 2), "rr": round(rr, 1),
                            "outcome": outcome, "r_mult": r_mult,
                            "atr_5m": round(atr_5m, 3),
                        })
                        fired_levels.add(level.name)
                        last_signal_bar = i
                        signals_today += 1
                        detected = True
                        break

                    # Rejection (score >= 8 only)
                    if level.score >= 8:
                        is_rej, rej_dir, strength = detect_rejection(last_5m, level, avg_vol_5m, cvd.prev_value, cvd.value, atr_5m)
                        if is_rej:
                            if rej_dir == "BULLISH":
                                entry = last_5m.c
                                stop = level.price - atr_5m * 0.3
                                tp_levels = sorted([l for l in levels if l.price > entry and l.price != level.price], key=lambda l: l.price)
                            else:
                                entry = last_5m.c
                                stop = level.price + atr_5m * 0.3
                                tp_levels = sorted([l for l in levels if l.price < entry and l.price != level.price], key=lambda l: l.price, reverse=True)

                            tp1 = tp_levels[0].price if tp_levels else (entry + atr_5m * 2 if rej_dir == "BULLISH" else entry - atr_5m * 2)
                            tp2 = tp_levels[1].price if len(tp_levels) > 1 else 0
                            risk = abs(entry - stop)
                            reward = abs(tp1 - entry)
                            rr = reward / risk if risk > 0 else 0
                            if rr < 2.0:
                                continue

                            outcome, exit_price, r_mult = evaluate_outcome(rej_dir, entry, stop, tp1, tp2, future_bars)
                            all_signals.append({
                                "date": date, "time": dt.strftime("%H:%M"), "asset": asset,
                                "pattern": f"REJECTION_{strength}", "direction": rej_dir,
                                "level": level.name, "score": level.score,
                                "entry": round(entry, 2), "stop": round(stop, 2),
                                "tp1": round(tp1, 2), "rr": round(rr, 1),
                                "outcome": outcome, "r_mult": r_mult,
                                "atr_5m": round(atr_5m, 3),
                            })
                            fired_levels.add(level.name)
                            last_signal_bar = i
                            signals_today += 1
                            break

        day_sigs = [s for s in all_signals if s["date"] == date]
        wins = sum(1 for s in day_sigs if "WIN" in s["outcome"])
        losses = sum(1 for s in day_sigs if "LOSS" in s["outcome"])
        expired = sum(1 for s in day_sigs if s["outcome"] == "EXPIRED")
        print(f"    {len(day_sigs)} signals | {wins}W {losses}L {expired}E")

    # ── REPORT ──
    print()
    print("=" * 70)
    print("  RESULTS")
    print("=" * 70)
    print(f"  Total signals: {len(all_signals)}")

    if not all_signals:
        print("  No signals.")
        return

    wins = [s for s in all_signals if "WIN" in s["outcome"]]
    losses = [s for s in all_signals if "LOSS" in s["outcome"]]
    expired = [s for s in all_signals if s["outcome"] == "EXPIRED"]
    decided = [s for s in all_signals if s["outcome"] != "EXPIRED"]

    win_rate = len(wins) / len(decided) * 100 if decided else 0
    avg_win_r = sum(s["r_mult"] for s in wins) / len(wins) if wins else 0
    avg_loss_r = sum(abs(s["r_mult"]) for s in losses) / len(losses) if losses else 1
    ev = (win_rate / 100 * avg_win_r) - ((100 - win_rate) / 100 * avg_loss_r)

    print(f"  Wins: {len(wins)} | Losses: {len(losses)} | Expired: {len(expired)}")
    print(f"  Win Rate: {win_rate:.1f}%")
    print(f"  Avg Win: {avg_win_r:.1f}R | Avg Loss: {avg_loss_r:.1f}R")
    print(f"  Expected Value: {ev:+.2f}R per trade")
    total_r = sum(s["r_mult"] for s in all_signals if s["outcome"] != "EXPIRED")
    print(f"  Total P&L: {total_r:+.1f}R across {len(decided)} trades")

    # By pattern
    print(f"\n  BY PATTERN:")
    for pat in sorted(set(s["pattern"] for s in all_signals)):
        ps = [s for s in all_signals if s["pattern"] == pat]
        pw = [s for s in ps if "WIN" in s["outcome"]]
        pl = [s for s in ps if "LOSS" in s["outcome"]]
        pd = pw + pl
        wr = len(pw) / len(pd) * 100 if pd else 0
        total = sum(s["r_mult"] for s in pd)
        print(f"    {pat:20s} {len(ps):3d} sigs | {len(pw)}W {len(pl)}L | WR {wr:.0f}% | P&L {total:+.1f}R")

    # By asset
    print(f"\n  BY ASSET:")
    for asset in ASSETS:
        asigs = [s for s in all_signals if s["asset"] == asset]
        if not asigs:
            continue
        aw = [s for s in asigs if "WIN" in s["outcome"]]
        al = [s for s in asigs if "LOSS" in s["outcome"]]
        ad = aw + al
        wr = len(aw) / len(ad) * 100 if ad else 0
        total = sum(s["r_mult"] for s in ad)
        print(f"    {asset:5s} {len(asigs):3d} sigs | {len(aw)}W {len(al)}L | WR {wr:.0f}% | P&L {total:+.1f}R")

    # By direction
    print(f"\n  BY DIRECTION:")
    for d in ["BULLISH", "BEARISH"]:
        ds = [s for s in all_signals if s["direction"] == d]
        dw = [s for s in ds if "WIN" in s["outcome"]]
        dl = [s for s in ds if "LOSS" in s["outcome"]]
        dd = dw + dl
        wr = len(dw) / len(dd) * 100 if dd else 0
        total = sum(s["r_mult"] for s in dd)
        print(f"    {d:8s} {len(ds):3d} sigs | {len(dw)}W {len(dl)}L | WR {wr:.0f}% | P&L {total:+.1f}R")

    # By score
    print(f"\n  BY LEVEL SCORE:")
    for sc in sorted(set(s["score"] for s in all_signals)):
        ss = [s for s in all_signals if s["score"] == sc]
        sw = [s for s in ss if "WIN" in s["outcome"]]
        sl = [s for s in ss if "LOSS" in s["outcome"]]
        sd = sw + sl
        wr = len(sw) / len(sd) * 100 if sd else 0
        total = sum(s["r_mult"] for s in sd)
        print(f"    Score {sc:2d}: {len(ss):3d} sigs | {len(sw)}W {len(sl)}L | WR {wr:.0f}% | P&L {total:+.1f}R")

    # By date
    print(f"\n  BY DATE:")
    for date in dates:
        ds = [s for s in all_signals if s["date"] == date]
        dw = [s for s in ds if "WIN" in s["outcome"]]
        dl = [s for s in ds if "LOSS" in s["outcome"]]
        dd = dw + dl
        wr = len(dw) / len(dd) * 100 if dd else 0
        total = sum(s["r_mult"] for s in dd)
        print(f"    {date}: {len(ds):3d} sigs | {len(dw)}W {len(dl)}L | WR {wr:.0f}% | P&L {total:+.1f}R")

    # Sample signals
    print(f"\n  ALL SIGNALS:")
    print(f"  {'Date':10s} {'Time':5s} {'Asset':5s} {'Pattern':20s} {'Dir':7s} {'Lvl':6s} {'Entry':>8s} {'Stop':>8s} {'TP1':>8s} {'RR':>4s} {'Result':10s} {'R':>5s}")
    print(f"  {'-' * 100}")
    for s in all_signals:
        print(f"  {s['date']:10s} {s['time']:5s} {s['asset']:5s} {s['pattern']:20s} {s['direction']:7s} {s['level']:6s} ${s['entry']:>7.2f} ${s['stop']:>7.2f} ${s['tp1']:>7.2f} {s['rr']:>3.1f} {s['outcome']:10s} {s['r_mult']:>+5.1f}")


if __name__ == "__main__":
    run_backtest()
