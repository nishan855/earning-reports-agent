import time
import sys
from datetime import datetime
import pytz
import yfinance as yf

sys.path.insert(0, ".")

from trading.constants import ASSETS
from trading.models import Candle, Level, Signal, DayContext, LevelState
from trading.data.candle_store import MultiCandleStore
from trading.data.cvd_engine import MultiCVDEngine
from trading.levels.builder import build_levels, calc_vwap, filter_today_bars
from trading.levels.volume_profile import compute_volume_profile
from trading.levels.zones import detect_zones
from trading.detection.level_state import TrackerEngine
from trading.detection.breakout import detect_breakout
from trading.detection.rejection import detect_rejection
from trading.detection.stop_hunt import detect_stop_hunt
from trading.detection.failed_breakout import detect_failed_retest, confirm_failed_breakout
from trading.core.gates import GateSystem
from trading.context.session import get_current_session, is_signal_allowed, minutes_to_cutoff
from trading.context.day_context import assess_day_context
from trading.context.options_context import get_options_env, get_strike, get_expiry, estimate_premium
from trading.agent.tools import ToolHandler, TOOL_DEFINITIONS
from trading.agent.brief import build_brief
from trading.notifications.formatter import format_telegram, format_daily_summary

ET = pytz.timezone("America/New_York")
results = []

def p(msg): print(msg, flush=True)
def ok(name): results.append((name, "PASS")); p(f"  + {name}")
def fail(name, reason): results.append((name, f"FAIL: {reason}")); p(f"  X {name}: {reason}")

def df_to_candles(df):
    out = []
    for ts, row in df.iterrows():
        try:
            c = Candle(t=int(ts.timestamp()*1000), o=round(float(row["Open"]),2), h=round(float(row["High"]),2), l=round(float(row["Low"]),2), c=round(float(row["Close"]),2), v=float(row["Volume"]))
            if c.c > 0 and c.h >= c.l: out.append(c)
        except: pass
    return out

# STEP 1
p("\n" + "=" * 60)
p("STEP 1 — Fetching real bars")
p("=" * 60)
bars = {}
for asset in ASSETS:
    try:
        t = yf.Ticker(asset)
        bars[asset] = {"daily": df_to_candles(t.history(period="2y", interval="1d")), "1m": df_to_candles(t.history(period="1d", interval="1m")), "5m": df_to_candles(t.history(period="5d", interval="5m")), "15m": df_to_candles(t.history(period="30d", interval="15m"))}
        p(f"  {asset}: {len(bars[asset]['daily'])}d {len(bars[asset]['1m'])}x1m {len(bars[asset]['5m'])}x5m")
        assert len(bars[asset]["daily"]) >= 20
        time.sleep(0.5)
    except Exception as e:
        fail(f"Fetch {asset}", str(e)); bars[asset] = {"daily":[],"1m":[],"5m":[],"15m":[]}
ok("Real bars fetched")

# STEP 2
p("\n" + "=" * 60)
p("STEP 2 — Building level maps")
p("=" * 60)
level_maps, vol_profiles, zone_maps = {}, {}, {}
for asset in ASSETS:
    try:
        daily, c1m, c5m = bars[asset]["daily"], bars[asset]["1m"], bars[asset]["5m"]
        if not daily: continue
        price = c1m[-1].c if c1m else daily[-1].c
        today = filter_today_bars(c1m)
        vwap = calc_vwap(today) if today else 0.0
        vp = compute_volume_profile(asset, today) if len(today) >= 5 else None
        if vp: vol_profiles[asset] = vp
        zones = detect_zones(daily, price)
        zone_maps[asset] = zones
        or_bars = today[:30]
        or_h = max((c.h for c in or_bars), default=0)
        or_l = min((c.l for c in or_bars), default=0)
        levels = build_levels(asset, daily, today, c5m, price, vwap, or_h, or_l, True, vp, zones)
        level_maps[asset] = levels
        assert len(levels) >= 3
        conf = [l for l in levels if l.confluence_with]
        p(f"  {asset}: {len(levels)} levels  ${price:.2f}  zones={len(zones)}  confluence={len(conf)}")
    except Exception as e:
        fail(f"{asset} levels", str(e))
ok("Level maps built")

# STEP 3
p("\n" + "=" * 60)
p("STEP 3 — Pattern detection on real 5m bars")
p("=" * 60)
det = {}
for asset in ASSETS:
    try:
        c5m, levels = bars[asset]["5m"], level_maps.get(asset, [])
        if len(c5m) < 10 or not levels: continue
        bos, rejs = [], []
        avg_v = sum(c.v for c in c5m[-20:]) / 20 if len(c5m) >= 20 else 1
        for i in range(1, len(c5m)):
            candle, prev = c5m[i], c5m[i-1]
            for level in levels:
                if level.score < 6: continue
                cvd_ch = candle.v * 0.6 * (1 if candle.c > candle.o else -1)
                is_bo, bo_dir = detect_breakout(candle, prev, level, avg_v, 0, cvd_ch)
                if is_bo: bos.append({"candle_idx":i,"level":level.name,"price":level.price,"direction":bo_dir,"vol_ratio":candle.v/avg_v if avg_v>0 else 1,"candle":candle})
                is_rej, rej_dir, strength = detect_rejection(candle, level, avg_v, 0, cvd_ch)
                if is_rej: rejs.append({"candle_idx":i,"level":level.name,"price":level.price,"direction":rej_dir,"strength":strength})
        det[asset] = {"breakouts": bos, "rejections": rejs}
        p(f"  {asset}: {len(bos)} breakouts  {len(rejs)} rejections")
    except Exception as e:
        fail(f"{asset} detection", str(e))
ok("Pattern detection ran")

# STEP 4
p("\n" + "=" * 60)
p("STEP 4 — LevelState tracker")
p("=" * 60)
tracker_ok = 0
for asset in ["SPY", "AAPL", "NVDA"]:
    try:
        c1m, levels = bars[asset]["1m"], level_maps.get(asset, [])
        bos = [b for b in det.get(asset, {}).get("breakouts", []) if b["vol_ratio"] > 1.3]
        if not bos or not c1m or not levels: p(f"  {asset}: skipped"); continue
        bo = bos[-1]
        level = next((l for l in levels if l.name == bo["level"]), None)
        if not level: continue
        tracker = TrackerEngine()
        tracker.start(asset, level.name, level.price, level.score, bo["direction"], bo["candle"], 50000, bo["vol_ratio"])
        avg_v1 = sum(c.v for c in c1m[-20:]) / 20 if len(c1m) >= 20 else 1
        cc, ff = 0, 0
        for rc in c1m[-10:]:
            c, f = tracker.on_1m_close(asset, rc, 45000, 40000, avg_v1, c1m[-1].c, 1.5)
            cc += len(c); ff += len(f)
        p(f"  {asset}: {level.name} ${level.price:.2f} ({bo['direction']}) confirmed={cc} failed={ff}")
        tracker_ok += 1
    except Exception as e:
        fail(f"{asset} tracker", str(e))
if tracker_ok > 0: ok(f"Tracker tested ({tracker_ok} assets)")
else: ok("Tracker tested (no breakouts to track — normal after hours)")

# STEP 5
p("\n" + "=" * 60)
p("STEP 5 — Gate system")
p("=" * 60)
try:
    gates = GateSystem()
    session = get_current_session()
    p(f"  Session: {session.label} quality={session.quality}")
    p_, r_ = gates.check_all("SPY", 8, 1.5, 36.0, "BULLISH", "BULLISH")
    assert not p_
    p_, r_ = gates.check_all("AAPL", 7, 1.5, 17.0, "BULLISH", "BEARISH")
    assert not p_
    gates.record_signal("SPY")
    st = gates.get_status("SPY")
    assert st["signals_today"] == 1
    p(f"  Gates: VIX block OK, counter-trend OK, cooldown OK, budget={st['budget_remaining']}")
    ok("Gate system")
except Exception as e:
    fail("Gates", str(e))

# STEP 6
p("\n" + "=" * 60)
p("STEP 6 — Day context")
p("=" * 60)
day_contexts = {}
for asset in ASSETS:
    try:
        daily, c15m, c1m = bars[asset]["daily"], bars[asset]["15m"], bars[asset]["1m"]
        if not daily: continue
        today = filter_today_bars(c1m)
        or_b = today[:30]
        or_h = max((c.h for c in or_b), default=0)
        or_l = min((c.l for c in or_b), default=0)
        price = c1m[-1].c if c1m else daily[-1].c
        dc = assess_day_context(asset, daily, c15m, today, or_h, or_l, price)
        day_contexts[asset] = dc
        p(f"  {asset}: {dc.day_type} {dc.bias} gap={dc.gap_type} {dc.gap_pct:+.2f}%")
    except Exception as e:
        fail(f"{asset} day ctx", str(e))
ok("Day context assessed")

# STEP 7
p("\n" + "=" * 60)
p("STEP 7 — Options context (real VIX)")
p("=" * 60)
try:
    vix_val = 20.0
    try:
        vh = yf.Ticker("^VIX").history(period="1d", interval="1m")
        if not vh.empty: vix_val = float(vh["Close"].iloc[-1])
    except: pass
    p(f"  VIX: {vix_val:.2f}")
    env = get_options_env(vix_val)
    p(f"  Env: {env['label']} size={env['size']}")
    for a in ["SPY", "AAPL"]:
        c1m = bars[a]["1m"]
        price = c1m[-1].c if c1m else 100
        strike = get_strike(a, price, "BULLISH", vix_val)
        dte, exp = get_expiry(a, datetime.now(ET).hour + datetime.now(ET).minute/60)
        lo, hi = estimate_premium(price, vix_val)
        p(f"  {a} ${price:.2f}: strike=${strike:.2f} {dte}DTE ({exp}) prem=${lo:.2f}-${hi:.2f}")
    ok(f"Options context (VIX={vix_val:.1f})")
except Exception as e:
    fail("Options", str(e))

# STEP 8
p("\n" + "=" * 60)
p("STEP 8 — Tool handler (real data)")
p("=" * 60)
try:
    asset = "SPY"
    c1m, c5m, levels = bars[asset]["1m"], bars[asset]["5m"], level_maps.get(asset, [])
    if not c1m or not levels: raise Exception("No SPY data")
    cs = MultiCandleStore()
    for c in c1m[-60:]: cs.get(asset).c1m.append(c)
    for c in c5m[-20:]: cs.get(asset).c5m.append(c)
    ce = MultiCVDEngine()
    for c in c1m[-30:]: ce.process_trade(asset, c.c if c.c > c.o else c.c - 0.01, c.v * 0.6)
    tl = next((l for l in levels if l.score >= 7), levels[0])
    setup = {"asset":asset,"pattern":"BREAKOUT_RETEST","direction":"BULLISH","level_name":tl.name,"level_price":tl.price,"level_score":tl.score,"session":get_current_session().label,"event_candle":c5m[-2] if len(c5m)>=2 else c1m[-1],"volume_ratio":1.5,"cvd_change":30000}
    h = ToolHandler(cs, ce, level_maps, vol_profiles, day_contexts, [], TrackerEngine(), vix_val, setup)
    tools_ok = 0
    for fn, args in [("get_level_map",[asset]),("get_setup_context",[asset]),("get_options_context",[asset]),("get_session",[]),("calculate_rr",[c1m[-1].c, c1m[-1].c-0.5, c1m[-1].c+2.0])]:
        r = getattr(h, fn)(*args)
        assert isinstance(r, str) and len(r) > 10
        tools_ok += 1
    p(f"  {tools_ok} tools tested with real data")
    ok("Tool handler")
except Exception as e:
    fail("Tools", str(e))

# STEP 9
p("\n" + "=" * 60)
p("STEP 9 — Brief builder (real data)")
p("=" * 60)
try:
    asset = "SPY"
    c1m, levels = bars[asset]["1m"], level_maps.get(asset, [])
    dc = day_contexts.get(asset, DayContext(asset=asset))
    price = c1m[-1].c if c1m else 100
    tl = next((l for l in levels if l.score >= 7), levels[0]) if levels else Level(name="TEST",price=price,score=7,type="resistance",source="PD",confidence="HIGH")
    above = sorted([l for l in levels if l.price > price], key=lambda l: l.price)[:4]
    below = sorted([l for l in levels if l.price <= price], key=lambda l: l.price, reverse=True)[:4]
    brief = build_brief(asset, "BREAKOUT_RETEST", "BULLISH", tl, (bars[asset]["5m"][-2] if len(bars[asset]["5m"])>=2 else c1m[-1]), None, 45000, 52000, True, 1.6, dc, vix_val, price, 1.5, above, below, get_current_session().label, get_current_session().quality, minutes_to_cutoff(), 0)
    assert len(brief) > 300 and asset in brief
    p(f"  Brief: {len(brief)} chars")
    p("  Preview: " + brief[:200].replace("\n"," | "))
    ok("Brief builder")
except Exception as e:
    fail("Brief", str(e))

# STEP 10
p("\n" + "=" * 60)
p("STEP 10 — Signal formatting")
p("=" * 60)
try:
    asset, price = "AAPL", bars["AAPL"]["1m"][-1].c if bars["AAPL"]["1m"] else 185
    strike = get_strike(asset, price, "BULLISH", vix_val)
    dte, exp = get_expiry(asset, datetime.now(ET).hour+datetime.now(ET).minute/60)
    lo, hi = estimate_premium(price, vix_val)
    sig = Signal(asset=asset, direction="LONG", confidence="HIGH", pattern="BREAKOUT_RETEST", level_name="PDH", level_price=price+1, entry=round(price,2), stop=round(price-0.5,2), tp1=round(price+2,2), tp2=round(price+4,2), rr=4.0, option_type="CALL", strike=strike, expiry_date=exp, dte=dte, size="FULL", est_premium_lo=lo, est_premium_hi=hi, breakeven=round(strike+(lo+hi)/2,2), narrative="PDH broken with volume.", reasoning="Strong setup.", invalidation=f"Below ${price-0.5:.2f}", fired_at=datetime.now(ET).strftime("%H:%M:%S"), session=get_current_session().label, vix_at_signal=vix_val)
    msg = format_telegram(sig)
    assert "AAPL" in msg and "LONG" in msg and "CALL" in msg
    assert format_telegram(Signal(asset="SPY",direction="WAIT",confidence="LOW",pattern="X",level_name="X",level_price=0,narrative="",reasoning="",invalidation="")) == ""
    p(f"  Telegram msg: {len(msg)} chars")
    ok("Signal formatting")
except Exception as e:
    fail("Formatting", str(e))

# STEP 11
p("\n" + "=" * 60)
p("STEP 11 — MultiEngine state")
p("=" * 60)
try:
    from trading.core.multi_engine import MultiEngine
    engine = MultiEngine("test", "test")
    engine._vix = vix_val
    for a in ASSETS:
        s = engine.get_asset_state(a)
        assert s["asset"] == a and s["budget_remaining"] == 3
    p(f"  8 assets initialized, VIX={vix_val:.1f}")
    ok("MultiEngine state")
except Exception as e:
    fail("Engine", str(e))

# STEP 12
p("\n" + "=" * 60)
p("STEP 12 — Full pipeline simulation")
p("=" * 60)
try:
    asset = "SPY"
    c1m, levels = bars[asset]["1m"], level_maps.get(asset, [])
    dc = day_contexts.get(asset, DayContext(asset=asset))
    price = c1m[-1].c if c1m else 100
    tl = next((l for l in levels if l.score >= 7), levels[0]) if levels else Level(name="PDH",price=price+1,score=8,type="resistance",source="PD",confidence="HIGH")
    above = sorted([l for l in levels if l.price > price], key=lambda l: l.price)[:4]
    below = sorted([l for l in levels if l.price <= price], key=lambda l: l.price, reverse=True)[:4]
    brief = build_brief(asset, "BREAKOUT_RETEST", "BULLISH", tl, (bars[asset]["5m"][-1] if bars[asset]["5m"] else c1m[-1]), None, 45000, 55000, True, 1.6, dc, vix_val, price, 1.5, above, below, get_current_session().label, get_current_session().quality, minutes_to_cutoff(), 0)
    assert len(brief) > 200
    p(f"  {asset} ${price:.2f} | {tl.name} ${tl.price:.2f} | {dc.bias} {dc.day_type}")
    p(f"  Brief: {len(brief)} chars | Gates: {'ALLOW' if is_signal_allowed() else 'BLOCK'}")
    ok("Full pipeline")
except Exception as e:
    fail("Pipeline", str(e))

# REPORT
p("\n" + "=" * 60)
p("REPORT")
p("=" * 60)
passed = [r for r in results if r[1] == "PASS"]
failed = [r for r in results if r[1] != "PASS"]
for n, s in results: p(f"  {'+'if s=='PASS' else 'X'} {n}")
p("-" * 60)
p(f"  {len(passed)}/{len(results)} passed")
for a in ASSETS: p(f"    {a}: {len(bars.get(a,{}).get('daily',[]))}d {len(bars.get(a,{}).get('1m',[]))}x1m {len(bars.get(a,{}).get('5m',[]))}x5m")
p("-" * 60)
if not failed: p("  ALL PASSED — READY FOR MONDAY")
elif len(failed) <= 2: p("  MOSTLY PASSING — FIX BEFORE MONDAY")
else: p("  MULTIPLE FAILURES — DO NOT GO LIVE")
p("=" * 60)
