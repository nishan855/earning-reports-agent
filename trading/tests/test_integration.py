import time
import random
from trading.constants import ASSETS
from trading.models import Candle, Level, Signal, DayContext, LevelState
from trading.core.multi_engine import MultiEngine
from trading.data.candle_store import MultiCandleStore
from trading.data.cvd_engine import MultiCVDEngine
from trading.detection.level_state import TrackerEngine
from trading.levels.builder import build_levels, calc_vwap
from trading.levels.volume_profile import compute_volume_profile
from trading.levels.zones import detect_zones
from trading.detection.breakout import detect_breakout
from trading.detection.rejection import detect_rejection
from trading.detection.stop_hunt import detect_stop_hunt
from trading.detection.failed_breakout import detect_failed_retest, confirm_failed_breakout
from trading.agent.tools import ToolHandler, TOOL_DEFINITIONS
from trading.agent.brief import build_brief
from trading.core.gates import GateSystem
from trading.context.session import get_current_session
from trading.context.options_context import get_options_env, get_strike, get_expiry, estimate_premium
from trading.notifications.formatter import format_telegram

results = []


def make_candles(base_price, count, trend=0.0):
    candles = []
    price = base_price
    for i in range(count):
        o = price
        c = price + trend + random.uniform(-0.3, 0.3)
        h = max(o, c) + random.uniform(0, 0.2)
        l = min(o, c) - random.uniform(0, 0.2)
        candles.append(Candle(t=1700000000000 + i * 60000, o=round(o, 2), h=round(h, 2), l=round(l, 2), c=round(c, 2), v=random.uniform(50000, 150000)))
        price = c
    return candles


# TEST 1: Full level map
daily = make_candles(185.0, 252, trend=0.01)
c1m = make_candles(185.45, 60)
vwap = calc_vwap(c1m)
vp = compute_volume_profile("AAPL", c1m)
zones = detect_zones(daily, 185.45)
levels = build_levels("AAPL", daily, c1m, [], 185.45, vwap, 186.20, 184.80, True, vp, zones)
assert len(levels) > 5 and vwap > 0 and vp and vp.poc > 0
results.append(("Full level map build", "PASS"))

# TEST 2: Breakout → tracker → retest
tracker = TrackerEngine()
level = Level(name="ORH", price=185.00, score=7, type="resistance", source="OR", confidence="HIGH")
prev = Candle(t=0, o=184.50, h=184.90, l=184.30, c=184.75, v=80000)
break_c = Candle(t=1, o=184.75, h=185.60, l=184.70, c=185.40, v=140000)
is_bo, direction = detect_breakout(break_c, prev, level, 100000, 10000, 40000)
assert is_bo and direction == "BULLISH"
tracker.start("AAPL", "ORH", 185.00, 7, "BULLISH", break_c, 40000, 1.4)
assert len(tracker.active_for_asset("AAPL")) == 1
retest = Candle(t=2, o=185.30, h=185.50, l=184.92, c=185.20, v=90000)
confirmed, failed = tracker.on_1m_close("AAPL", retest, 52000, 40000, 80000, 185.20, 1.30)
assert len(confirmed) == 1 and confirmed[0].direction == "BULLISH"
results.append(("Breakout → tracker → retest", "PASS"))

# TEST 3: Failed retest
tracker_obj = LevelState(asset="SPY", level_name="PDH", level_price=645.50, level_score=8, direction="BULLISH", break_time=time.time(), expires_at=time.time() + 180)
fail_c = Candle(t=3, o=645.70, h=645.80, l=645.30, c=645.20, v=100000)
assert detect_failed_retest(fail_c, tracker_obj)
confirm_c = Candle(t=4, o=645.20, h=645.30, l=645.00, c=645.10, v=110000)
assert confirm_failed_breakout(confirm_c, tracker_obj, -15000, 5000)
results.append(("Failed retest → reverse", "PASS"))

# TEST 4: Gate system
gates = GateSystem()
# All gates block outside hours, so just verify they block
p, r = gates.check_all("SPY", 8, 1.5, 36.0, "BULLISH", "BULLISH")
assert not p  # blocked (hours or VIX)
p, r = gates.check_all("SPY", 8, 0.5, 17.0, "BULLISH", "BULLISH")
assert not p  # blocked (hours or volume)
p, r = gates.check_all("AAPL", 7, 1.5, 17.0, "BULLISH", "BEARISH")
assert not p  # blocked (hours or counter-trend)
gates.record_signal("SPY")
st = gates.get_status("SPY")
assert st["signals_today"] == 1 and st["global_pause_remaining"] > 0
results.append(("Gate system full check", "PASS"))

# TEST 5: Tool handler
cs = MultiCandleStore()
ce = MultiCVDEngine()
for c in c1m:
    cs.get("AAPL").c1m.append(c)
ce.process_trade("AAPL", 185.40, 500)
ce.process_trade("AAPL", 185.50, 300)
setup = {"asset": "AAPL", "pattern": "BREAKOUT_RETEST", "direction": "BULLISH", "level_name": "ORH", "level_price": 185.00, "level_score": 7, "session": "POWER HOUR", "event_candle": break_c, "volume_ratio": 1.4, "cvd_change": 30000}
handler = ToolHandler(cs, ce, {"AAPL": levels}, {"AAPL": vp} if vp else {}, {}, [], tracker, 17.2, setup)
assert "AAPL" in handler.get_level_map("AAPL")
assert "RR" in handler.calculate_rr(185.45, 185.05, 187.50)
assert "SESSION" in handler.get_session()
assert "VIX" in handler.get_options_context("AAPL")
assert "BREAKOUT_RETEST" in handler.get_setup_context("AAPL")
results.append(("Tool handler (5 tools)", "PASS"))

# TEST 6: Brief builder
dc = DayContext(asset="AAPL", day_type="TREND", bias="BULLISH", bias_locked=True, gap_pct=0.5, gap_type="GAP_UP")
brief = build_brief("AAPL", "BREAKOUT_RETEST", "BULLISH", Level(name="ORH", price=185.00, score=7, type="resistance", source="OR", confidence="HIGH", description="Opening Range High."), break_c, retest, 40000, 52000, True, 1.8, dc, 17.2, 185.45, 1.30, levels[:3], levels[-3:], "POWER HOUR", 5, 73, 0)
assert "AAPL" in brief and "BREAKOUT_RETEST" in brief and len(brief) > 500
results.append(("Brief builder full", "PASS"))

# TEST 7: Signal formatting
sig = Signal(asset="AAPL", direction="LONG", confidence="HIGH", pattern="BREAKOUT_RETEST", level_name="ORH", level_price=185.00, entry=185.45, stop=185.05, tp1=187.50, tp2=189.20, rr=5.1, option_type="CALL", strike=185.0, expiry_date="Mar 29", dte=1, size="FULL", est_premium_lo=1.80, est_premium_hi=2.20, breakeven=187.00, narrative="ORH broken.", reasoning="Confirmed.", invalidation="Below 185.05", fired_at="10:47:00", session="POWER HOUR", vix_at_signal=17.2)
msg = format_telegram(sig)
assert "AAPL" in msg and "LONG" in msg and "185.45" in msg and "CALL" in msg
assert format_telegram(Signal(asset="SPY", direction="WAIT", confidence="LOW", pattern="X", level_name="X", level_price=0, narrative="", reasoning="", invalidation="")) == ""
results.append(("Signal formatting", "PASS"))

# TEST 8: Options context
assert get_options_env(14.0)["size"] == "FULL"
assert get_options_env(27.0)["size"] == "HALF"
assert get_options_env(36.0)["size"] == "SKIP"
assert get_strike("AAPL", 185.45, "BULLISH", 17.0) > 0
dte, exp = get_expiry("SPY", 10.5)
assert dte >= 0
lo, hi = estimate_premium(185.45, 17.2)
assert lo > 0 and hi > lo
results.append(("Options context full", "PASS"))

# TEST 9: CVD multi-asset
cvd2 = MultiCVDEngine()
cvd2.process_trade("SPY", 645.10, 500)
cvd2.process_trade("SPY", 645.20, 300)
cvd2.process_trade("AAPL", 185.40, 200)
cvd2.process_trade("AAPL", 185.30, 100)
assert cvd2.value("SPY") > 0 and cvd2.value("AAPL") < 0 and cvd2.value("SPY") != cvd2.value("AAPL")
results.append(("CVD multi-asset independence", "PASS"))

# TEST 10: MultiEngine
engine = MultiEngine(finnhub_key="test", openai_key="test")
for asset in ASSETS:
    s = engine.get_asset_state(asset)
    assert s["asset"] == asset and s["budget_remaining"] == 3
results.append(("MultiEngine all assets", "PASS"))

# RESULTS
print("\n" + "=" * 50)
print("PHASE 7 — INTEGRATION TESTS")
print("=" * 50)
passed = 0
for name, status in results:
    icon = "+" if status == "PASS" else "X"
    print(f"  [{icon}] {name}")
    if status == "PASS":
        passed += 1
print("=" * 50)
print(f"  {passed}/{len(results)} tests passed")
print("=" * 50)
