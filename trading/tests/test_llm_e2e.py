"""
END-TO-END LLM TEST — 5 real scenarios through the full pipeline.
Uses real yfinance data → real detection → real brief → real GPT-5.4 agent → real tools → signal.
"""
import sys, os, asyncio, json, time
sys.path.insert(0, ".")

import yfinance as yf
import pytz
from datetime import datetime

from trading.models import Candle, Level, DayContext, CVDPoint, VolumeProfile
from trading.levels.builder import build_levels, calc_vwap
from trading.levels.volume_profile import compute_volume_profile
from trading.levels.zones import detect_zones
from trading.detection.breakout import detect_breakout
from trading.detection.rejection import detect_rejection
from trading.detection.stop_hunt import detect_stop_hunt
from trading.context.day_context import assess_day_context
from trading.data.candle_store import AssetCandleStore, MultiCandleStore
from trading.data.cvd_engine import AssetCVDEngine, MultiCVDEngine
from trading.agent.tools import ToolHandler
from trading.agent.brief import build_brief
from trading.agent.agent import run_agent
from trading.constants import MIN_RR, ASSETS

ET = pytz.timezone("America/New_York")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")


def to_candles(df):
    out = []
    for ts, row in df.iterrows():
        try:
            c = Candle(t=int(ts.timestamp()*1000), o=round(float(row["Open"]),2),
                       h=round(float(row["High"]),2), l=round(float(row["Low"]),2),
                       c=round(float(row["Close"]),2), v=float(row["Volume"]))
            if c.c > 0 and c.h >= c.l:
                out.append(c)
        except:
            pass
    return out


def filter_market_hours(bars):
    return [c for c in bars if 570 <= datetime.fromtimestamp(c.t/1000, tz=ET).hour * 60 + datetime.fromtimestamp(c.t/1000, tz=ET).minute < 960]


def filter_before(bars, end_min):
    return [c for c in bars if datetime.fromtimestamp(c.t/1000, tz=ET).hour * 60 + datetime.fromtimestamp(c.t/1000, tz=ET).minute < end_min]


def get_trading_days(bars):
    days = {}
    for c in bars:
        et = datetime.fromtimestamp(c.t / 1000, tz=ET)
        t = et.hour * 60 + et.minute
        if 570 <= t < 960:
            day = et.strftime("%Y-%m-%d")
            days.setdefault(day, []).append(c)
    return days


def find_scenarios(asset_data, max_scenarios=5):
    """Scan all data, compute RR for each detection, then pick a mix:
    ~half GOOD (RR >= 2.5, high score) and ~half BAD (RR < 1.5 or weak score).
    This tests whether the agent correctly trades the good and skips the bad."""
    all_found = []
    scan_times = [615, 630, 645, 660, 690, 720, 750, 780, 810, 840, 870, 900]

    for asset in ASSETS:
        d = asset_data[asset]
        for day_str, day_bars in sorted(d["days"].items()):
            for scan_time in scan_times:
                before = filter_before(day_bars, scan_time)
                if len(before) < 6:
                    continue

                price = before[-1].c
                or_bars = [c for c in day_bars if datetime.fromtimestamp(c.t/1000, tz=ET).hour * 60 + datetime.fromtimestamp(c.t/1000, tz=ET).minute < 600]
                or_h = max(c.h for c in or_bars) if or_bars else 0
                or_l = min(c.l for c in or_bars) if or_bars else 0
                vwap = calc_vwap(before)
                vp = compute_volume_profile(asset, before) if len(before) >= 5 else None
                zones = detect_zones(d["daily"], price)
                levels = build_levels(asset, d["daily"], before, before, price, vwap, or_h, or_l, scan_time >= 600, vp, zones)
                if not levels:
                    continue

                atr = sum(c.h - c.l for c in before[-14:]) / min(len(before[-14:]), 14)
                avg_vol = sum(c.v for c in before[-20:]) / max(len(before[-20:]), 1)
                candle, prev = before[-1], before[-2]
                cvd = sum(c.v * ((c.c - c.l) / max(c.h - c.l, 0.01) - 0.5) * 2 for c in before)
                cvd_sc = candle.v * 0.6 * (1 if candle.c > candle.o else -1) / 1000
                avg_sc = avg_vol / 1000

                for level in levels:
                    if level.score < 6:
                        continue
                    detectors = [
                        ("BREAKOUT_RETEST", lambda l=level: detect_breakout(candle, prev, l, avg_sc, 0, cvd_sc, atr)),
                        ("REJECTION", lambda l=level: detect_rejection(candle, l, avg_sc, 0, cvd_sc, atr)),
                        ("STOP_HUNT", lambda l=level: detect_stop_hunt(candle, l, avg_sc, cvd_sc, atr)),
                    ]
                    for pat_name, detect_fn in detectors:
                        try:
                            is_det, direction = detect_fn()
                            if not is_det or not direction:
                                continue

                            # Compute RR for classification
                            if direction == "BULLISH":
                                stop = level.price - atr * 0.5
                                tgts = sorted([l for l in levels if l.price > price], key=lambda l: l.price)
                                target = tgts[0].price if tgts else price + atr * 3
                            else:
                                stop = level.price + atr * 0.5
                                tgts = sorted([l for l in levels if l.price < price], key=lambda l: l.price, reverse=True)
                                target = tgts[0].price if tgts else price - atr * 3
                            risk = abs(price - stop)
                            reward = abs(target - price)
                            rr = reward / risk if risk > 0 else 0

                            sc = {
                                "asset": asset, "day": day_str, "scan_time": scan_time,
                                "pattern": pat_name, "direction": direction,
                                "level": level, "candle": candle, "prev": prev,
                                "price": price, "atr": atr, "avg_vol": avg_vol,
                                "cvd": cvd, "vwap": vwap, "or_h": or_h, "or_l": or_l,
                                "levels": levels, "daily": d["daily"], "day_bars": before,
                                "vp": vp, "zones": zones,
                                "_rr": rr, "_score": level.score,
                            }
                            all_found.append(sc)
                            break  # one per level
                        except Exception:
                            pass

    # Classify: GOOD = RR >= 2.5 + score >= 8, BAD = RR < 1.5 or score == 6
    good = [s for s in all_found if s["_rr"] >= 2.5 and s["_score"] >= 8]
    bad = [s for s in all_found if s["_rr"] < 1.5 or s["_score"] == 6]

    # Deduplicate by asset — pick best/worst per asset
    def pick_diverse(pool, n):
        picked = []
        used = set()
        for s in pool:
            if s["asset"] not in used:
                picked.append(s)
                used.add(s["asset"])
                if len(picked) >= n:
                    break
        # If not enough unique assets, allow duplicates
        if len(picked) < n:
            for s in pool:
                if s not in picked:
                    picked.append(s)
                    if len(picked) >= n:
                        break
        return picked

    # Sort good by highest RR, bad by lowest RR
    good.sort(key=lambda s: -s["_rr"])
    bad.sort(key=lambda s: s["_rr"])

    n_good = min(3, len(good))
    n_bad = min(max_scenarios - n_good, len(bad))
    n_good = min(max_scenarios - n_bad, len(good))  # rebalance

    selected = pick_diverse(good, n_good) + pick_diverse(bad, n_bad)
    # Interleave: good, bad, good, bad...
    g = [s for s in selected if s["_rr"] >= 2.5 and s["_score"] >= 8]
    b = [s for s in selected if s not in g]
    interleaved = []
    gi, bi = 0, 0
    while gi < len(g) or bi < len(b):
        if gi < len(g): interleaved.append(g[gi]); gi += 1
        if bi < len(b): interleaved.append(b[bi]); bi += 1

    print(f"  Total detections: {len(all_found)} | Good (RR>=2.5, score>=8): {len(good)} | Bad (RR<1.5 or score=6): {len(bad)}")
    for s in interleaved:
        tag = "GOOD" if s in g else "BAD"
        print(f"    [{tag}] {s['asset']} {s['pattern']} {s['direction']} {s['level'].name} score={s['_score']} RR={s['_rr']:.1f}")

    return interleaved[:max_scenarios]


def build_test_stores(scenario):
    """Build real candle store + CVD engine from scenario data, using system classes."""
    asset = scenario["asset"]

    # Candle store
    candle_store = MultiCandleStore()
    store = candle_store.get(asset)
    # Load bars into the store using real methods
    store.c1m = scenario["day_bars"] + [scenario["candle"]]  # last one is "live"
    store.c5m = scenario["day_bars"][::5] + [scenario["candle"]]
    store.c15m = scenario["day_bars"][::15] + [scenario["candle"]]
    store.c_daily = scenario["daily"]

    # CVD engine — simulate from candle data
    cvd_engine = MultiCVDEngine()
    eng = cvd_engine.get(asset)
    eng._session_date = scenario["day"]
    eng._total_volume = sum(c.v for c in scenario["day_bars"])
    eng._cvd = scenario["cvd"]
    # Build some history points
    for i, c in enumerate(scenario["day_bars"][-15:]):
        et = datetime.fromtimestamp(c.t / 1000, tz=ET)
        minute_key = et.strftime("%H:%M")
        delta = c.v * ((c.c - c.l) / max(c.h - c.l, 0.01) - 0.5) * 2
        eng._history.append(CVDPoint(
            time_et=minute_key,
            value=eng._cvd + delta * (i - 15),
            delta=delta,
        ))
    eng._current_minute = eng._history[-1].time_et if eng._history else ""

    return candle_store, cvd_engine


def build_test_handler(scenario, candle_store, cvd_engine):
    """Build real ToolHandler from scenario data."""
    asset = scenario["asset"]

    # Level store
    level_store = {asset: scenario["levels"]}

    # Volume profiles
    vol_profiles = {}
    if scenario["vp"]:
        vol_profiles[asset] = scenario["vp"]

    # Day context using real function
    daily = scenario["daily"]
    day_bars = scenario["day_bars"]
    day_context = assess_day_context(
        asset=asset, daily_bars=daily,
        bars_15m=day_bars[::15] if len(day_bars) > 15 else day_bars,
        bars_1m_today=day_bars,
        or_high=scenario["or_h"], or_low=scenario["or_l"],
        current_price=scenario["price"],
    )
    day_contexts = {asset: day_context}

    # Setup context
    current_setup = {
        "asset": asset,
        "pattern": scenario["pattern"],
        "direction": scenario["direction"],
        "level_name": scenario["level"].name,
        "level_price": scenario["level"].price,
        "level_score": scenario["level"].score,
        "event_candle": scenario["candle"],
        "volume_ratio": scenario["candle"].v / max(scenario["avg_vol"], 1),
        "cvd_change": scenario["cvd"],
        "session": "POWER HOUR" if scenario["scan_time"] < 660 else "MID MORNING",
    }

    handler = ToolHandler(
        candle_store=candle_store,
        cvd_engine=cvd_engine,
        level_store=level_store,
        vol_profiles=vol_profiles,
        day_contexts=day_contexts,
        signal_history=[],
        tracker_engine=None,
        vix_value=18.5,  # simulated normal VIX
        current_setup=current_setup,
    )
    return handler, day_context


def build_test_brief(scenario, day_context):
    """Build real brief using the system's build_brief()."""
    asset = scenario["asset"]
    levels = scenario["levels"]
    price = scenario["price"]
    level = scenario["level"]

    nearest_above = sorted([l for l in levels if l.price > price], key=lambda l: l.price)[:4]
    nearest_below = sorted([l for l in levels if l.price <= price], key=lambda l: l.price, reverse=True)[:4]

    h = scenario["scan_time"] // 60
    m = scenario["scan_time"] % 60
    session_name = "POWER HOUR" if scenario["scan_time"] < 660 else "MID MORNING"
    session_quality = 5 if scenario["scan_time"] < 660 else 4
    minutes_to_cutoff = 930 - scenario["scan_time"]

    brief = build_brief(
        asset=asset,
        pattern=scenario["pattern"],
        direction=scenario["direction"],
        level=level,
        event_candle=scenario["candle"],
        retest_candle=scenario["prev"],
        cvd_at_break=scenario["cvd"] * 0.8,
        cvd_now=scenario["cvd"],
        cvd_turned=True,
        volume_ratio=scenario["candle"].v / max(scenario["avg_vol"], 1),
        day_context=day_context,
        vix=18.5,
        current_price=price,
        atr=scenario["atr"],
        nearest_above=nearest_above,
        nearest_below=nearest_below,
        session_name=session_name,
        session_quality=session_quality,
        minutes_to_cutoff=minutes_to_cutoff,
        tests_today=0,
    )
    return brief


async def run_single_test(idx, scenario):
    """Run one full E2E test: data → brief → GPT-5.4 agent → signal."""
    asset = scenario["asset"]
    print(f"\n{'='*80}")
    print(f"  CASE {idx+1}: {asset} {scenario['pattern']} {scenario['direction']}")
    print(f"  Day: {scenario['day']} | Time: {scenario['scan_time']//60}:{scenario['scan_time']%60:02d}")
    print(f"  Level: {scenario['level'].name} ${scenario['level'].price:.2f} (score {scenario['level'].score})")
    print(f"  Price: ${scenario['price']:.2f} | ATR: ${scenario['atr']:.2f}")
    print(f"{'='*80}")

    # Build real stores
    candle_store, cvd_engine = build_test_stores(scenario)

    # Build real handler
    handler, day_context = build_test_handler(scenario, candle_store, cvd_engine)

    # Build real brief
    brief = build_test_brief(scenario, day_context)
    print(f"\n  BRIEF ({len(brief)} chars):")
    for line in brief.split("\n")[:15]:
        print(f"    {line}")
    print(f"    ... ({len(brief.split(chr(10)))} total lines)")

    # Track tool calls
    tool_log = []
    tokens = []

    async def on_token(tok):
        tokens.append(tok)

    async def on_tool_call(name, status, args, result):
        if status == "complete":
            tool_log.append({"tool": name, "args": args, "result": result[:200]})
            print(f"    TOOL: {name}({json.dumps(args)[:60]}) → {result[:100]}")

    signal_result = {"signal": None}

    async def on_complete(sig):
        signal_result["signal"] = sig

    async def on_error(err):
        print(f"    ERROR: {err}")
        signal_result["signal"] = "ERROR: " + err

    # Call real GPT-5.4 agent
    print(f"\n  Calling GPT-5.4 agent...")
    start = time.time()

    await run_agent(
        tool_handler=handler,
        initial_brief=brief,
        openai_key=OPENAI_KEY,
        on_token=on_token,
        on_tool_call=on_tool_call,
        on_complete=on_complete,
        on_error=on_error,
    )

    elapsed = time.time() - start
    sig = signal_result["signal"]

    print(f"\n  RESULT ({elapsed:.1f}s, {len(tool_log)} tool calls):")
    if sig and hasattr(sig, "direction"):
        print(f"    Decision:    {sig.direction} ({sig.confidence})")
        print(f"    Pattern:     {sig.pattern}")
        if sig.direction in ("LONG", "SHORT"):
            print(f"    Entry:       ${sig.entry:.2f}")
            print(f"    Stop:        ${sig.stop:.2f}")
            print(f"    TP1:         ${sig.tp1:.2f}")
            print(f"    RR:          {sig.rr:.1f}:1")
            print(f"    Option:      {sig.option_type} ${sig.strike:.2f} exp {sig.expiry_date}")
            print(f"    Size:        {sig.size}")
            print(f"    Instrument:  {sig.instrument}")
            print(f"    Narrative:   {sig.narrative[:120]}")
            print(f"    Reasoning:   {sig.reasoning[:120]}")
            print(f"    Invalidation:{sig.invalidation[:120]}")
        else:
            print(f"    Wait reason: {sig.wait_for}")
            print(f"    Narrative:   {sig.narrative[:120]}")
    elif sig:
        print(f"    {sig}")
    else:
        reasoning = "".join(tokens)
        print(f"    Agent text: {reasoning[:200]}")

    return {
        "case": idx + 1,
        "asset": asset,
        "pattern": scenario["pattern"],
        "direction": scenario["direction"],
        "level": scenario["level"].name,
        "price": scenario["price"],
        "signal": sig,
        "tool_calls": len(tool_log),
        "elapsed": round(elapsed, 1),
        "tools_used": [t["tool"] for t in tool_log],
    }


async def main():
    if not OPENAI_KEY:
        print("ERROR: OPENAI_API_KEY not set")
        return

    print("=" * 80)
    print("  FULL E2E LLM TEST — 5 CASES")
    print("  Pipeline: yfinance → detection → brief → GPT-5.4 agent → tools → signal")
    print("=" * 80)

    # Fetch real data
    print("\nFetching real data...")
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

    # Find 5 real scenarios where detection fires
    print("\nScanning for scenarios with real detections...")
    scenarios = find_scenarios(asset_data, max_scenarios=5)
    print(f"  Found {len(scenarios)} scenarios")

    if not scenarios:
        print("  No detections found in recent data. Try expanding scan times.")
        return

    # Run each through full LLM pipeline
    results = []
    for i, sc in enumerate(scenarios):
        result = await run_single_test(i, sc)
        results.append(result)

    # Summary
    print(f"\n{'='*80}")
    print(f"  SUMMARY — {len(results)} CASES")
    print(f"{'='*80}")
    print(f"  {'#':>2} {'Asset':5} {'Pattern':12} {'Dir':7} {'Level':8} {'Decision':8} {'Conf':6} {'RR':>4} {'Tools':>5} {'Time':>5}")
    print(f"  {'-'*75}")

    longs, shorts, waits, errors = 0, 0, 0, 0
    for r in results:
        sig = r["signal"]
        if sig and hasattr(sig, "direction"):
            dec = sig.direction
            conf = sig.confidence
            rr = f"{sig.rr:.1f}" if sig.rr > 0 else "-"
            if dec == "LONG": longs += 1
            elif dec == "SHORT": shorts += 1
            elif dec == "WAIT": waits += 1
        elif isinstance(sig, str) and "ERROR" in sig:
            dec, conf, rr = "ERROR", "-", "-"
            errors += 1
        else:
            dec, conf, rr = "NONE", "-", "-"

        print(f"  {r['case']:>2} {r['asset']:5} {r['pattern']:12} {r['direction'][:7]:7} "
              f"{r['level']:8} {dec:8} {conf:6} {rr:>4} {r['tool_calls']:>5} {r['elapsed']:>4.1f}s")

    print(f"\n  LONG: {longs} | SHORT: {shorts} | WAIT: {waits} | ERROR: {errors}")
    avg_time = sum(r["elapsed"] for r in results) / len(results) if results else 0
    avg_tools = sum(r["tool_calls"] for r in results) / len(results) if results else 0
    print(f"  Avg time: {avg_time:.1f}s | Avg tools: {avg_tools:.1f}")

    tools_used = {}
    for r in results:
        for t in r["tools_used"]:
            tools_used[t] = tools_used.get(t, 0) + 1
    print(f"  Tools used: {dict(sorted(tools_used.items(), key=lambda x: -x[1]))}")

    print(f"\n{'='*80}")
    print(f"  END E2E TEST")
    print(f"{'='*80}")


if __name__ == "__main__":
    asyncio.run(main())
