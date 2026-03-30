"""Replay NVDA March 26 STOP_HUNT with full LLM logging."""
import sys, os, asyncio, time, json
sys.path.insert(0, ".")
import yfinance as yf
import pytz
from datetime import datetime
from trading.models import Candle, CVDPoint
from trading.levels.builder import build_levels, calc_vwap
from trading.levels.volume_profile import compute_volume_profile
from trading.levels.zones import detect_zones
from trading.detection.stop_hunt import detect_stop_hunt
from trading.context.day_context import assess_day_context
from trading.data.candle_store import MultiCandleStore
from trading.data.cvd_engine import MultiCVDEngine
from trading.agent.tools import ToolHandler
from trading.agent.brief import build_brief
from trading.agent.agent import run_agent

ET = pytz.timezone("America/New_York")
KEY = os.environ["OPENAI_API_KEY"]
SEP = "=" * 80
LINE = "-" * 80

def to_candles(df):
    out = []
    for ts, row in df.iterrows():
        try:
            c = Candle(t=int(ts.timestamp()*1000), o=round(float(row["Open"]),2), h=round(float(row["High"]),2),
                       l=round(float(row["Low"]),2), c=round(float(row["Close"]),2), v=float(row["Volume"]))
            if c.c > 0 and c.h >= c.l: out.append(c)
        except Exception: pass
    return out


async def main():
    t = yf.Ticker("NVDA")
    c5m = to_candles(t.history(period="5d", interval="5m"))
    daily = to_candles(t.history(period="2y", interval="1d"))
    mkt = [c for c in c5m if 570 <= datetime.fromtimestamp(c.t/1000, tz=ET).hour*60+datetime.fromtimestamp(c.t/1000, tz=ET).minute < 960]
    days = {}
    for c in mkt:
        d = datetime.fromtimestamp(c.t/1000, tz=ET).strftime("%Y-%m-%d")
        days.setdefault(d, []).append(c)
    sorted_days = sorted(days.keys())
    target = sorted_days[-2]  # March 26
    day_bars = days[target]
    print(f"Replaying NVDA {target} ({len(day_bars)} bars)\n")

    for end_idx in range(10, len(day_bars)):
        before = day_bars[:end_idx]
        price = before[-1].c
        et_dt = datetime.fromtimestamp(before[-1].t/1000, tz=ET)
        scan_min = et_dt.hour*60+et_dt.minute
        if scan_min < 600:
            continue
        or_bars = [c for c in day_bars if datetime.fromtimestamp(c.t/1000, tz=ET).hour*60+datetime.fromtimestamp(c.t/1000, tz=ET).minute < 600]
        or_h = max(c.h for c in or_bars) if or_bars else 0
        or_l = min(c.l for c in or_bars) if or_bars else 0
        vwap = calc_vwap(before)
        vp = compute_volume_profile("NVDA", before)
        zones = detect_zones(daily, price)
        levels = build_levels("NVDA", daily, before, before, price, vwap, or_h, or_l, True, vp, zones)
        if not levels:
            continue
        atr = sum(c.h-c.l for c in before[-14:])/min(len(before[-14:]),14)
        avg_vol = sum(c.v for c in before[-20:])/max(len(before[-20:]),1)
        candle, prev = before[-1], before[-2]
        cvd = sum(c.v*((c.c-c.l)/max(c.h-c.l,0.01)-0.5)*2 for c in before)
        cvd_sc = candle.v*0.6*(1 if candle.c>candle.o else -1)/1000
        avg_sc = avg_vol/1000
        vol_ratio = candle.v/max(avg_vol,1)

        for lv in levels:
            if lv.score < 6:
                continue
            try:
                ok, d = detect_stop_hunt(candle, lv, avg_sc, cvd_sc, atr)
            except Exception:
                continue
            if not ok or not d:
                continue

            dc = assess_day_context("NVDA", daily, before[::3], before, or_h, or_l, price)
            near_a = sorted([l for l in levels if l.price > price], key=lambda l: l.price)[:4]
            near_b = sorted([l for l in levels if l.price <= price], key=lambda l: l.price, reverse=True)[:4]
            brief = build_brief(
                asset="NVDA", pattern="STOP_HUNT", direction=d, level=lv,
                event_candle=candle, retest_candle=prev, cvd_at_break=cvd*0.8, cvd_now=cvd,
                cvd_turned=True, volume_ratio=vol_ratio, day_context=dc, vix=20.0,
                current_price=price, atr=atr, nearest_above=near_a, nearest_below=near_b,
                session_name="POWER HOUR", session_quality=5, minutes_to_cutoff=330, tests_today=0,
            )

            print(SEP)
            print("STEP 1: DETECTION")
            print(f"  Pattern:   STOP_HUNT {d}")
            print(f"  Level:     {lv.name} ${lv.price:.2f} (score {lv.score})")
            print(f"  Time:      {et_dt.strftime('%H:%M')} ET")
            print(f"  Price:     ${price:.2f}")
            print(f"  Vol ratio: {vol_ratio:.2f}x")
            print(SEP)

            print(f"\nSTEP 2: BRIEF SENT TO GPT-5.4")
            print(LINE)
            print(brief)
            print(LINE)

            # Build stores
            cs = MultiCandleStore()
            s = cs.get("NVDA")
            s.c1m = before + [candle]
            s.c5m = before + [candle]
            s.c15m = before[::3] + [candle]
            s.c_daily = daily
            ce = MultiCVDEngine()
            e = ce.get("NVDA")
            e._cvd = cvd
            e._total_volume = sum(c.v for c in before)
            e._session_date = et_dt.strftime("%Y-%m-%d")
            for j, c2 in enumerate(before[-15:]):
                dt2 = datetime.fromtimestamp(c2.t/1000, tz=ET)
                delta = c2.v*((c2.c-c2.l)/max(c2.h-c2.l,0.01)-0.5)*2
                e._history.append(CVDPoint(time_et=dt2.strftime("%H:%M"), value=e._cvd+delta*(j-15), delta=delta))
            e._current_minute = e._history[-1].time_et if e._history else ""

            setup = {
                "asset": "NVDA", "pattern": "STOP_HUNT", "direction": d,
                "level_name": lv.name, "level_price": lv.price, "level_score": lv.score,
                "session": "POWER HOUR", "event_candle": candle,
                "volume_ratio": vol_ratio, "cvd_change": cvd,
            }
            handler = ToolHandler(cs, ce, {"NVDA": levels}, {"NVDA": vp} if vp else {},
                                  {"NVDA": dc}, [], None, 20.0, setup)

            tokens = []

            async def on_token(tok):
                tokens.append(tok)

            async def on_tool(name, status, args, result):
                if status == "complete":
                    print(f"\nSTEP 3: TOOL CALL -> {name}")
                    print(f"  Args:   {json.dumps(args)[:500]}")
                    print(f"  Result: {result[:500]}")

            sig_box = [None]

            async def on_complete(sig):
                sig_box[0] = sig

            async def on_error(err):
                print(f"ERROR: {err}")

            await run_agent(handler, brief, KEY,
                            on_token=on_token, on_tool_call=on_tool,
                            on_complete=on_complete, on_error=on_error)

            reasoning = "".join(tokens)
            sig = sig_box[0]

            if reasoning:
                print(f"\nSTEP 3a: AGENT REASONING (streamed text)")
                print(LINE)
                print(reasoning)
                print(LINE)

            if sig:
                print(f"\n{SEP}")
                print("STEP 4: FINAL SIGNAL")
                print(f"  Decision:     {sig.direction} ({sig.confidence})")
                print(f"  Entry:        ${sig.entry:.2f}")
                print(f"  Stop:         ${sig.stop:.2f}")
                print(f"  TP1:          ${sig.tp1:.2f}")
                if sig.tp2 > 0:
                    print(f"  TP2:          ${sig.tp2:.2f}")
                print(f"  RR:           {sig.rr:.1f}:1")
                print(f"  Option:       {sig.option_type} ${sig.strike:.2f} exp {sig.expiry_date}")
                print(f"  Instrument:   {sig.instrument}")
                print(f"  Size:         {sig.size}")
                print(f"  Narrative:    {sig.narrative}")
                print(f"  Reasoning:    {sig.reasoning}")
                print(f"  Invalidation: {sig.invalidation}")
                if sig.warnings:
                    print(f"  Warnings:     {sig.warnings}")
                print(SEP)
            return


asyncio.run(main())
