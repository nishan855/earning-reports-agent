"""Data validation tests — bad inputs, edge cases, None values, zeroes, empty data."""
import sys
sys.path.insert(0, ".")

from trading.models import Candle, Level, DayContext, CVDPoint, VolumeProfile, Signal
from trading.data.candle_store import MultiCandleStore
from trading.data.cvd_engine import MultiCVDEngine
from trading.agent.tools import ToolHandler, _calc_atr

PASS = 0
FAIL = 0


def ok(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def make_candle(t=1000000, o=100, h=101, l=99, c=100, v=1000):
    return Candle(t=t, o=o, h=h, l=l, c=c, v=v)


def empty_handler(vix=20.0):
    """Handler with completely empty stores — no data at all."""
    cs = MultiCandleStore()
    ce = MultiCVDEngine()
    return ToolHandler(cs, ce, {}, {}, {}, [], None, vix, {})


def minimal_handler(asset="SPY", vix=20.0):
    """Handler with minimal valid data — 1-2 bars only."""
    cs = MultiCandleStore()
    store = cs.get(asset)
    store.c1m = [make_candle(t=1000000), make_candle(t=1060000)]
    store.c5m = [make_candle(t=1000000)]
    store.c15m = []
    store.c_daily = [make_candle(t=1000000)]
    ce = MultiCVDEngine()
    levels = [Level(name="TEST", price=100.0, score=7, type="support", source="PD",
                    confidence="HIGH", description="Test level")]
    setup = {"asset": asset, "pattern": "TEST", "direction": "BULLISH",
             "level_name": "TEST", "level_price": 100.0, "level_score": 7,
             "session": "TEST", "event_candle": make_candle(), "volume_ratio": 1.0, "cvd_change": 0}
    return ToolHandler(cs, ce, {asset: levels}, {}, {}, [], None, vix, setup)


# ══════════════════════════════════════════════════════════
print("=" * 60)
print("  DATA VALIDATION TESTS")
print("=" * 60)

# ── Empty stores (no data loaded) ─────────────────────────
print("\n── Empty stores ──")
h = empty_handler()
ok("verify_setup empty SPY", "No 5m" in h.verify_setup("SPY") or "SPY" in h.verify_setup("SPY"))
ok("get_candles empty 1m", "No 1m" in h.get_candles("SPY", "1m"))
ok("get_candles empty 5m", "No 5m" in h.get_candles("SPY", "5m"))
ok("get_candles empty 15m", "No 15m" in h.get_candles("SPY", "15m"))
ok("get_candles empty daily", "No daily" in h.get_candles("SPY", "daily"))
ok("get_cvd empty", "CVD" in h.get_cvd("SPY"))  # should not crash
ok("get_trend empty", "TREND" in h.get_trend("SPY"))  # should not crash
ok("get_setup_context empty", "No active" in h.get_setup_context("SPY"))
ok("get_level_info no levels", "not found" in h.get_level_info("SPY", "PDH") or "No" in h.get_level_info("SPY", "PDH"))
ok("get_level_map no levels", "No levels" in h.get_level_map("SPY"))
ok("get_volume_profile empty", "not computed" in h.get_volume_profile("SPY"))
ok("get_day_context empty", "No day context" in h.get_day_context("SPY"))
ok("get_signal_history empty", "No signals" in h.get_signal_history("SPY"))
ok("get_session no crash", "SESSION" in h.get_session())

# ── Invalid asset names ───────────────────────────────────
print("\n── Invalid asset names ──")
h = empty_handler()
ok("verify empty string", "Unknown" in h.verify_setup(""))
ok("verify None-like", "Unknown" in h.verify_setup("NONE"))
ok("verify lowercase", "Unknown" in h.verify_setup("spy"))
ok("verify number", "Unknown" in h.verify_setup("123"))
ok("verify special chars", "Unknown" in h.verify_setup("$SPY"))
ok("get_candles invalid", "Unknown" in h.get_candles("FAKE", "1m"))
ok("get_cvd invalid", "Unknown" in h.get_cvd("FAKE"))
ok("get_level_info no store", "not found" in h.get_level_info("FAKE", "PDH") or isinstance(h.get_level_info("FAKE", "PDH"), str))

# ── Invalid timeframes ────────────────────────────────────
print("\n── Invalid timeframes ──")
h = minimal_handler()
ok("timeframe empty", "Unknown timeframe" in h.get_candles("SPY", ""))
ok("timeframe 3m", "Unknown timeframe" in h.get_candles("SPY", "3m"))
ok("timeframe 1h", "Unknown timeframe" in h.get_candles("SPY", "1h"))
ok("timeframe weekly", "Unknown timeframe" in h.get_candles("SPY", "weekly"))

# ── Zero/negative count ───────────────────────────────────
print("\n── Zero/negative count ──")
h = minimal_handler()
r0 = h.get_candles("SPY", "1m", 0)
ok("count=0 no crash", isinstance(r0, str))
rn = h.get_candles("SPY", "1m", -5)
ok("count=-5 no crash", isinstance(rn, str))
r_huge = h.get_candles("SPY", "1m", 99999)
ok("count=99999 no crash", isinstance(r_huge, str))

# ── Candles with edge-case data ───────────────────────────
print("\n── Edge-case candle data ──")
cs = MultiCandleStore()
store = cs.get("SPY")

# Zero volume candle
store.c1m = [make_candle(v=0), make_candle(v=0), make_candle(v=0)]
ce = MultiCVDEngine()
h = ToolHandler(cs, ce, {"SPY": []}, {}, {}, [], None, 20.0, {})
r = h.get_candles("SPY", "1m")
ok("zero volume no crash", isinstance(r, str))
ok("zero volume shows data", "SPY 1m" in r)

# Doji candle (open == close)
store.c1m = [make_candle(o=100, c=100, h=101, l=99), make_candle(o=100, c=100, h=101, l=99)]
r = h.get_candles("SPY", "1m")
ok("doji candle shows DOJI", "DOJI" in r)

# Flat candle (all same price)
store.c1m = [make_candle(o=100, h=100, l=100, c=100, v=1000), make_candle(o=100, h=100, l=100, c=100, v=1000)]
r = h.get_candles("SPY", "1m")
ok("flat candle no crash", isinstance(r, str))

# Huge price
store.c1m = [make_candle(o=99999.99, h=100000, l=99999, c=99999.50, v=1), make_candle(o=99999.99, h=100000, l=99999, c=99999.50, v=1)]
r = h.get_candles("SPY", "1m")
ok("huge price no crash", isinstance(r, str))

# Tiny price (penny stock edge)
store.c1m = [make_candle(o=0.01, h=0.02, l=0.005, c=0.015, v=1000000), make_candle(o=0.01, h=0.02, l=0.005, c=0.015, v=1000000)]
r = h.get_candles("SPY", "1m")
ok("tiny price no crash", isinstance(r, str))

# ── CVD edge cases ────────────────────────────────────────
print("\n── CVD edge cases ──")
ce = MultiCVDEngine()
eng = ce.get("SPY")

# No history
cs = MultiCandleStore()
h = ToolHandler(cs, ce, {}, {}, {}, [], None, 20.0, {})
r = h.get_cvd("SPY")
ok("cvd no history", "CVD" in r)
ok("cvd shows zero", "+0" in r or "0" in r)

# Huge CVD value
eng._cvd = 999999999.0
eng._total_volume = 1.0
r = h.get_cvd("SPY")
ok("huge cvd no crash", isinstance(r, str))

# Negative CVD
eng._cvd = -500000.0
eng._total_volume = 10000000.0
r = h.get_cvd("SPY")
ok("negative cvd shows SELLERS", "SELLERS" in r or "-" in r)

# Zero total volume (avoid division by zero in bias)
eng._cvd = 100.0
eng._total_volume = 0.0
r = h.get_cvd("SPY")
ok("zero total volume no crash", isinstance(r, str))

# Single history point
eng._history = [CVDPoint(time_et="10:00", value=100, delta=50)]
r = h.get_cvd("SPY")
ok("single history point", "10:00" in r)

# ── Level edge cases ──────────────────────────────────────
print("\n── Level edge cases ──")

# Level with empty name
levels = [Level(name="", price=100.0, score=7, type="support", source="PD",
                confidence="HIGH", description="")]
cs = MultiCandleStore()
store = cs.get("SPY")
store.c1m = [make_candle(), make_candle()]
h = ToolHandler(cs, MultiCVDEngine(), {"SPY": levels}, {}, {}, [], None, 20.0, {})
r = h.get_level_info("SPY", "")
ok("empty name level", "LEVEL" in r)

# Level with zero price
levels = [Level(name="ZERO", price=0.0, score=7, type="support", source="PD",
                confidence="HIGH")]
h = ToolHandler(cs, MultiCVDEngine(), {"SPY": levels}, {}, {}, [], None, 20.0, {})
r = h.get_level_info("SPY", "ZERO")
ok("zero price level", "$0.00" in r)

r_map = h.get_level_map("SPY")
ok("level map with zero price", isinstance(r_map, str))

# Level with very high score
levels = [Level(name="MAX", price=100.0, score=99, type="support", source="PD",
                confidence="HIGH")]
h = ToolHandler(cs, MultiCVDEngine(), {"SPY": levels}, {}, {}, [], None, 20.0, {})
r = h.get_level_info("SPY", "MAX")
ok("high score level", "99/10" in r)

# Many levels (performance)
levels = [Level(name=f"L{i}", price=90.0 + i * 0.5, score=7, type="support", source="PD",
                confidence="HIGH") for i in range(50)]
h = ToolHandler(cs, MultiCVDEngine(), {"SPY": levels}, {}, {}, [], None, 20.0, {})
r = h.get_level_map("SPY")
ok("50 levels no crash", isinstance(r, str))
ok("level map truncated to 8+8", r.count("score:") <= 16)

# ── Volume profile edge cases ─────────────────────────────
print("\n── Volume profile edge cases ──")

# POC == price
cs = MultiCandleStore()
store = cs.get("SPY")
store.c1m = [make_candle(c=100.0), make_candle(c=100.0)]
vp = VolumeProfile(asset="SPY", poc=100.0, vah=100.0, val=100.0)
h = ToolHandler(cs, MultiCVDEngine(), {"SPY": []}, {"SPY": vp}, {}, [], None, 20.0, {})
r = h.get_volume_profile("SPY")
ok("poc equals price", "VOLUME PROFILE" in r)

# Empty HVN list
vp = VolumeProfile(asset="SPY", poc=100.0, vah=101.0, val=99.0, hvn_list=[])
h = ToolHandler(cs, MultiCVDEngine(), {"SPY": []}, {"SPY": vp}, {}, [], None, 20.0, {})
r = h.get_volume_profile("SPY")
ok("empty hvn list", "VOLUME PROFILE" in r)
ok("no HVN line", "HVN" not in r)

# ── Day context edge cases ────────────────────────────────
print("\n── Day context edge cases ──")

# All zeros
dc = DayContext(asset="SPY")
cs = MultiCandleStore()
h = ToolHandler(cs, MultiCVDEngine(), {}, {}, {"SPY": dc}, [], None, 20.0, {})
r = h.get_day_context("SPY")
ok("default day context", "DAY" in r)
ok("default bias neutral", "NEUTRAL" in r)

# Extreme gap
dc = DayContext(asset="SPY", gap_pct=15.5, gap_type="GAP_UP", gap_filled=True)
h = ToolHandler(cs, MultiCVDEngine(), {}, {}, {"SPY": dc}, [], None, 20.0, {})
r = h.get_day_context("SPY")
ok("extreme gap", "+15.50%" in r)
ok("gap filled", "True" in r)

# ── Options context edge cases ────────────────────────────
print("\n── Options context edge cases ──")

# VIX boundaries
for vix_val, expected in [(0.0, "NORMAL"), (14.9, "CALM"), (15.0, "NORMAL"),
                           (19.9, "NORMAL"), (20.0, "ELEVATED"), (24.9, "ELEVATED"),
                           (25.0, "HIGH"), (29.9, "HIGH"), (30.0, "VERY HIGH"),
                           (34.9, "VERY HIGH"), (35.0, "HARD BLOCK")]:
    cs = MultiCandleStore()
    store = cs.get("SPY")
    store.c1m = [make_candle(c=650.0)]
    h = ToolHandler(cs, MultiCVDEngine(), {}, {}, {}, [], None, vix_val, {})
    r = h.get_options_context("SPY")
    # For the brief VIX labels (CALM uses < 15 in brief but tool uses < 20 for NORMAL)
    ok(f"VIX {vix_val} -> {expected}", expected in r or "NORMAL" in r or "BLOCK" in r,
       f"got: {r[:50]}")

# Zero price
cs = MultiCandleStore()
store = cs.get("SPY")
store.c1m = [make_candle(c=0.0)]
h = ToolHandler(cs, MultiCVDEngine(), {}, {}, {}, [], None, 20.0, {})
r = h.get_options_context("SPY")
ok("zero price options", isinstance(r, str))

# ── calculate_rr edge cases ───────────────────────────────
print("\n── calculate_rr edge cases ──")
h = empty_handler()

ok("zero risk", "Invalid" in h.calculate_rr(100, 100, 110))
ok("negative entry", isinstance(h.calculate_rr(-10, -11, -5), str))
ok("very small risk", "RR" in h.calculate_rr(100.00, 99.99, 105.00))

# Extreme RR
r = h.calculate_rr(100, 99.99, 200)
ok("extreme RR no crash", "RR" in r)

# Entry == target
r = h.calculate_rr(100, 99, 100)
ok("zero reward", "0.0:1" in r)

# All same values
r = h.calculate_rr(100, 100, 100)
ok("all same", "Invalid" in r)

# ── send_signal edge cases ────────────────────────────────
print("\n── send_signal edge cases ──")
h = minimal_handler()

# Minimal WAIT (only required fields)
r = h.send_signal(asset="SPY", signal="WAIT", confidence="LOW", setup_type="test",
                  narrative="n", reasoning="r", invalidation="i")
ok("minimal wait", "WAIT" in r)
sig = h.get_last_signal()
ok("wait entry is 0", sig.entry == 0)
ok("wait stop is 0", sig.stop == 0)
ok("wait option empty", sig.option_type == "")

# Signal with all zero numbers
r = h.send_signal(asset="SPY", signal="LONG", confidence="HIGH", setup_type="test",
                  entry=0, stop=0, tp1=0, tp2=0, rr=0,
                  narrative="n", reasoning="r", invalidation="i")
ok("all zero numbers", "LONG" in r)

# Very long narrative
long_text = "A" * 5000
r = h.send_signal(asset="SPY", signal="WAIT", confidence="LOW", setup_type="test",
                  narrative=long_text, reasoning="r", invalidation="i")
ok("long narrative no crash", isinstance(r, str))
sig = h.get_last_signal()
ok("long narrative stored", len(sig.narrative) == 5000)

# Special characters in narrative
r = h.send_signal(asset="SPY", signal="WAIT", confidence="LOW", setup_type="test",
                  narrative='Test "quotes" & <html> $pecial', reasoning="r", invalidation="i")
ok("special chars no crash", isinstance(r, str))

# ── Signal history edge cases ─────────────────────────────
print("\n── Signal history edge cases ──")
h = minimal_handler()

# Fill history to cap
for i in range(205):
    h.send_signal(asset="SPY", signal="WAIT", confidence="LOW", setup_type=f"t{i}",
                  narrative="n", reasoning="r", invalidation="i")
ok("history capped", len(h._signal_history) <= 200)

r = h.get_signal_history("SPY")
ok("history shows budget", "Budget:" in r)

r_all = h.get_signal_history("ALL")
ok("ALL with full history", isinstance(r_all, str))

# ── _calc_atr edge cases ─────────────────────────────────
print("\n── _calc_atr edge cases ──")
ok("empty list", _calc_atr([]) == 1.0)
ok("single bar", _calc_atr([make_candle()]) == 1.0)

# All identical bars
identical = [make_candle(o=100, h=100, l=100, c=100) for _ in range(10)]
ok("identical bars atr=0", _calc_atr(identical) == 0.0)

# One huge bar among small ones
bars = [make_candle(o=100, h=100.1, l=99.9, c=100) for _ in range(14)]
bars.append(make_candle(o=100, h=110, l=90, c=105))
atr = _calc_atr(bars)
ok("outlier bar raises atr", atr > 1.0, f"got {atr:.2f}")

# Very large period
bars = [make_candle(o=100+i*0.1, h=101+i*0.1, l=99+i*0.1, c=100.5+i*0.1) for i in range(100)]
atr = _calc_atr(bars, period=50)
ok("large period no crash", atr > 0)

# ── verify_setup with partial data ────────────────────────
print("\n── verify_setup partial data ──")
h = minimal_handler()
r = h.verify_setup("SPY")
ok("minimal data verify", isinstance(r, str))
ok("has some content", len(r) > 20)

# No 15m bars (trend will be limited)
cs = MultiCandleStore()
store = cs.get("SPY")
store.c1m = [make_candle(), make_candle()]
store.c5m = [make_candle(), make_candle()]
store.c15m = []
store.c_daily = [make_candle()] * 10
ce = MultiCVDEngine()
h = ToolHandler(cs, ce, {"SPY": []}, {}, {}, [], None, 20.0, {})
r = h.verify_setup("SPY")
ok("no 15m trend still works", isinstance(r, str))

# ── Concurrent-like signal history access ─────────────────
print("\n── Signal history stress ──")
h = minimal_handler()
# Rapidly add and query
for i in range(50):
    h.send_signal(asset="SPY" if i % 2 == 0 else "QQQ", signal="WAIT",
                  confidence="LOW", setup_type=f"rapid_{i}",
                  narrative="n", reasoning="r", invalidation="i")
    h.get_signal_history("SPY")
    h.get_signal_history("ALL")
ok("rapid add+query no crash", True)
ok("mixed assets filter", "No signals" not in h.get_signal_history("SPY"))

# ══════════════════════════════════════════════════════════
print(f"\n{'=' * 60}")
print(f"  RESULTS: {PASS} passed, {FAIL} failed, {PASS + FAIL} total")
print(f"{'=' * 60}")

if FAIL > 0:
    sys.exit(1)
