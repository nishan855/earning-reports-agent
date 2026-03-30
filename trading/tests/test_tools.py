"""Comprehensive tests for all ToolHandler methods."""
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


def make_candle(t, o, h, l, c, v):
    return Candle(t=t, o=o, h=h, l=l, c=c, v=v)


def build_handler(asset="SPY", price=650.0, vix=20.0, setup=None):
    """Build a ToolHandler with realistic test data."""
    cs = MultiCandleStore()
    store = cs.get(asset)

    # 1m bars — 20 bars around price
    bars_1m = []
    for i in range(22):
        p = price - 2.0 + i * 0.2
        bars_1m.append(make_candle(
            t=1711000000000 + i * 60000,
            o=p, h=p + 0.15, l=p - 0.10, c=p + 0.05, v=100000 + i * 5000,
        ))
    store.c1m = bars_1m

    # 5m bars
    bars_5m = []
    for i in range(12):
        p = price - 3.0 + i * 0.5
        bars_5m.append(make_candle(
            t=1711000000000 + i * 300000,
            o=p, h=p + 0.40, l=p - 0.25, c=p + 0.15, v=500000 + i * 20000,
        ))
    store.c5m = bars_5m

    # 15m bars
    bars_15m = []
    for i in range(8):
        p = price - 4.0 + i * 1.0
        bars_15m.append(make_candle(
            t=1711000000000 + i * 900000,
            o=p, h=p + 0.80, l=p - 0.50, c=p + 0.30, v=1500000 + i * 50000,
        ))
    store.c15m = bars_15m

    # Daily bars
    daily = []
    for i in range(10):
        p = price - 10 + i * 2
        daily.append(make_candle(
            t=1710800000000 + i * 86400000,
            o=p, h=p + 3.0, l=p - 2.0, c=p + 1.0, v=50000000,
        ))
    store.c_daily = daily

    # CVD engine
    ce = MultiCVDEngine()
    eng = ce.get(asset)
    eng._cvd = 50000.0
    eng._total_volume = 5000000.0
    eng._session_date = "2026-03-27"
    eng._current_minute = "10:30"
    for i in range(15):
        eng._history.append(CVDPoint(
            time_et=f"10:{15+i:02d}",
            value=40000 + i * 1000,
            delta=800 + i * 50,
        ))

    # Levels
    levels = [
        Level(name="PDH", price=price + 2.0, score=10, type="resistance", source="PD",
              confidence="HIGH", description="Previous day high"),
        Level(name="PDL", price=price - 3.0, score=8, type="support", source="PD",
              confidence="HIGH", description="Previous day low"),
        Level(name="ORH", price=price + 1.0, score=9, type="resistance", source="OR",
              confidence="HIGH", description="Opening range high"),
        Level(name="ORL", price=price - 1.5, score=9, type="support", source="OR",
              confidence="HIGH", description="Opening range low"),
        Level(name="VWAP", price=price - 0.5, score=4, type="support", source="VWAP",
              confidence="MEDIUM", description="VWAP"),
        Level(name="POC", price=price + 0.3, score=8, type="resistance", source="VOLUME",
              confidence="HIGH", description="Point of control", confluence_with=["ORH"]),
    ]
    level_store = {asset: levels}

    # Volume profile
    vp = VolumeProfile(asset=asset, poc=price + 0.3, vah=price + 1.5, val=price - 1.0,
                       hvn_list=[price + 0.3, price - 0.8], computed_at="10:30")
    vol_profiles = {asset: vp}

    # Day context
    dc = DayContext(asset=asset, day_type="RANGE", bias="BEARISH", bias_locked=True,
                    gap_pct=-0.5, gap_type="GAP_DOWN", gap_filled=False,
                    or_high=price + 1.0, or_low=price - 1.5, or_complete=True,
                    relative_str=-0.3)
    day_contexts = {asset: dc}

    # Setup
    if setup is None:
        setup = {
            "asset": asset, "pattern": "BREAKOUT_RETEST", "direction": "BEARISH",
            "level_name": "ORH", "level_price": price + 1.0, "level_score": 9,
            "session": "POWER HOUR",
            "event_candle": bars_1m[-2], "volume_ratio": 1.5, "cvd_change": -5000,
        }

    handler = ToolHandler(cs, ce, level_store, vol_profiles, day_contexts,
                          [], None, vix, setup)
    return handler, levels


# ══════════════════════════════════════════════════════════
print("=" * 60)
print("  TOOL HANDLER — COMPREHENSIVE TESTS")
print("=" * 60)

# ── verify_setup ──────────────────────────────────────────
print("\n── verify_setup ──")
h, _ = build_handler()
result = h.verify_setup("SPY")
ok("returns string", isinstance(result, str))
ok("contains candle data", "SPY 5m" in result)
ok("contains CVD data", "CVD" in result)
ok("contains trend", "TREND" in result)
ok("contains volume profile", "VOL PROFILE" in result or "POC" in result)
ok("unknown asset", "Unknown" in h.verify_setup("FAKE"))

# ── get_candles ───────────────────────────────────────────
print("\n── get_candles ──")
h, _ = build_handler()
r1m = h.get_candles("SPY", "1m", 5)
ok("1m returns bars", "SPY 1m" in r1m)
ok("1m has OHLC", "O:" in r1m and "H:" in r1m)
ok("1m has color", "GREEN" in r1m or "RED" in r1m)
ok("1m has vol ratio", "vol:" in r1m)
ok("1m has wick/body", "wick/body:" in r1m)
ok("1m respects count", r1m.count("\n") <= 6)  # header + 5 bars

r5m = h.get_candles("SPY", "5m", 3)
ok("5m returns bars", "SPY 5m" in r5m)

r15m = h.get_candles("SPY", "15m")
ok("15m returns bars", "SPY 15m" in r15m)

rd = h.get_candles("SPY", "daily", 5)
ok("daily returns bars", "SPY daily" in rd)

ok("unknown asset", "Unknown asset" in h.get_candles("FAKE", "1m"))
ok("unknown timeframe", "Unknown timeframe" in h.get_candles("SPY", "3m"))

# Empty store
cs2 = MultiCandleStore()
h2 = ToolHandler(cs2, MultiCVDEngine(), {"SPY": []}, {}, {}, [], None, 20.0, {})
ok("no bars message", "No 1m bars" in h2.get_candles("SPY", "1m"))

# ── get_cvd ───────────────────────────────────────────────
print("\n── get_cvd ──")
h, _ = build_handler()
rc = h.get_cvd("SPY")
ok("returns CVD header", "SPY CVD:" in rc)
ok("shows current value", "+50,000" in rc or "50,000" in rc)
ok("shows bias", "BUYERS" in rc or "SELLERS" in rc or "NEUTRAL" in rc)
ok("shows history", "10:" in rc)
ok("shows trend", "RISING" in rc or "FALLING" in rc or "FLAT" in rc)
ok("shows divergence check", "divergence" in rc.lower() or "DIVERGENCE" in rc)
ok("respects minutes param", isinstance(h.get_cvd("SPY", 5), str))
ok("unknown asset", "Unknown" in h.get_cvd("FAKE"))

# ── get_setup_context ─────────────────────────────────────
print("\n── get_setup_context ──")
h, _ = build_handler()
rs = h.get_setup_context("SPY")
ok("returns setup", "SETUP" in rs)
ok("shows pattern", "BREAKOUT_RETEST" in rs)
ok("shows direction", "BEARISH" in rs)
ok("shows level", "ORH" in rs)
ok("shows score", "9/10" in rs)
ok("shows candle", "O:" in rs)
ok("shows volume", "1.5x" in rs)
ok("wrong asset", "No active setup" in h.get_setup_context("QQQ"))

# ── get_level_info ────────────────────────────────────────
print("\n── get_level_info ──")
h, levels = build_handler()
rl = h.get_level_info("SPY", "PDH")
ok("returns level", "LEVEL" in rl)
ok("shows price", "$652.00" in rl)
ok("shows score", "10/10" in rl)
ok("shows type", "resistance" in rl)
ok("shows source", "PD" in rl)
ok("shows description", "Previous day high" in rl)
ok("shows tests", "Tests today: 0" in rl)
ok("shows fresh note", "fresh" in rl)

rl_conf = h.get_level_info("SPY", "POC")
ok("shows confluence", "CONFLUENCE" in rl_conf)

ok("level not found", "not found" in h.get_level_info("SPY", "FAKE_LEVEL"))
ok("lists available", "PDH" in h.get_level_info("SPY", "FAKE_LEVEL"))

# ── get_level_map ─────────────────────────────────────────
print("\n── get_level_map ──")
h, _ = build_handler()
rm = h.get_level_map("SPY")
ok("shows header", "LEVELS" in rm)
ok("shows price", "PRICE" in rm)
ok("shows resistance", "RESISTANCE" in rm)
ok("shows support", "SUPPORT" in rm)
ok("shows ATR distance", "ATR" in rm)
ok("shows level names", "PDH" in rm)
ok("shows scores", "score:" in rm)
ok("shows confluence", "*CONF" in rm)

ok("no levels", "No levels" in ToolHandler(
    MultiCandleStore(), MultiCVDEngine(), {"SPY": []}, {}, {}, [], None, 20.0, {}
).get_level_map("SPY"))

# ── get_volume_profile ────────────────────────────────────
print("\n── get_volume_profile ──")
h, _ = build_handler()
rv = h.get_volume_profile("SPY")
ok("shows header", "VOLUME PROFILE" in rv)
ok("shows POC", "POC" in rv)
ok("shows VAH", "VAH" in rv)
ok("shows VAL", "VAL" in rv)
ok("shows position", "ABOVE" in rv or "BELOW" in rv)
ok("shows value area", "VALUE AREA" in rv or "VA" in rv)
ok("shows HVN", "HVN" in rv)

ok("no profile", "not computed" in ToolHandler(
    MultiCandleStore(), MultiCVDEngine(), {}, {}, {}, [], None, 20.0, {}
).get_volume_profile("SPY"))

# ── get_trend ─────────────────────────────────────────────
print("\n── get_trend ──")
h, _ = build_handler()
rt = h.get_trend("SPY")
ok("shows header", "TREND" in rt)
ok("shows daily", "Daily:" in rt)
ok("shows 15m", "15m:" in rt)
ok("has direction", "BULLISH" in rt or "BEARISH" in rt or "NEUTRAL" in rt)

# ── get_day_context ───────────────────────────────────────
print("\n── get_day_context ──")
h, _ = build_handler()
rd = h.get_day_context("SPY")
ok("shows header", "DAY" in rd)
ok("shows type", "RANGE" in rd)
ok("shows bias", "BEARISH" in rd)
ok("shows gap", "GAP_DOWN" in rd)
ok("shows gap pct", "-0.50%" in rd)
ok("shows OR", "OR:" in rd)
ok("shows relative str", "Relative strength" in rd)
ok("no context", "No day context" in h.get_day_context("QQQ"))

# ── get_options_context ───────────────────────────────────
print("\n── get_options_context ──")
h, _ = build_handler(vix=18.0)
ro = h.get_options_context("SPY")
ok("shows header", "OPTIONS" in ro)
ok("shows VIX", "18.0" in ro)
ok("shows env", "NORMAL" in ro)
ok("shows size", "FULL" in ro)
ok("shows strike", "ATM strike" in ro)
ok("shows DTE", "DTE" in ro)
ok("shows premium", "Est premium" in ro)
ok("shows breakeven", "Break-even" in ro)

h_high, _ = build_handler(vix=25.0)
ro_high = h_high.get_options_context("SPY")
ok("elevated VIX", "ELEVATED" in ro_high or "HIGH" in ro_high)

h_block, _ = build_handler(vix=36.0)
ro_block = h_block.get_options_context("SPY")
ok("VIX hard block", "HARD BLOCK" in ro_block)

# ── get_session ───────────────────────────────────────────
print("\n── get_session ──")
h, _ = build_handler()
rs = h.get_session()
ok("returns string", isinstance(rs, str))
ok("shows session name", "SESSION:" in rs)
ok("shows quality", "Quality:" in rs)
ok("shows cutoff", "cutoff" in rs.lower())
ok("shows close", "close" in rs.lower())

# ── get_signal_history ────────────────────────────────────
print("\n── get_signal_history ──")
h, _ = build_handler()
rh = h.get_signal_history("SPY")
ok("no signals", "No signals" in rh)

# Add a signal to history
h._signal_history.append(Signal(
    asset="SPY", direction="SHORT", confidence="HIGH", pattern="BREAKOUT_RETEST",
    level_name="ORH", level_price=651.0, entry=650.5, stop=651.5, tp1=648.0,
    rr=2.5, fired_at="10:35:00", session="POWER HOUR", vix_at_signal=20.0,
))
rh2 = h.get_signal_history("SPY")
ok("shows signal", "SHORT" in rh2)
ok("shows asset", "SPY" in rh2)
ok("shows budget", "Budget:" in rh2)
ok("shows 1 used", "1/" in rh2)

rh_all = h.get_signal_history("ALL")
ok("ALL returns all", "SPY" in rh_all)

ok("other asset empty", "No signals" in h.get_signal_history("QQQ"))

# ── calculate_rr ──────────────────────────────────────────
print("\n── calculate_rr ──")
h, _ = build_handler()
rr1 = h.calculate_rr(650.0, 649.0, 654.0)
ok("shows RR", "RR: 4.0:1" in rr1)
ok("shows risk", "Risk $1.00" in rr1)
ok("shows reward", "Reward $4.00" in rr1)
ok("acceptable", "ACCEPTABLE" in rr1)

rr2 = h.calculate_rr(650.0, 649.0, 650.5)
ok("low RR insufficient", "INSUFFICIENT" in rr2)
ok("shows min target", "Min target" in rr2)

rr3 = h.calculate_rr(650.0, 650.0, 655.0)
ok("zero risk", "Invalid" in rr3)

# Short direction
rr4 = h.calculate_rr(650.0, 651.0, 646.0)
ok("short RR", "RR: 4.0:1" in rr4)

# ── send_signal ───────────────────────────────────────────
print("\n── send_signal ──")
h, _ = build_handler()

# LONG signal
r_long = h.send_signal(
    asset="SPY", signal="LONG", confidence="HIGH", setup_type="BREAKOUT_RETEST",
    entry=650.0, stop=649.0, tp1=654.0, tp2=656.0, rr=4.0,
    option_type="CALL", strike=650.0, expiry_date="Mar 30", dte=1,
    size="FULL", est_premium_lo=2.50, est_premium_hi=3.50,
    instrument="ATM outright", narrative="Bullish breakout confirmed",
    reasoning="Trend aligned, CVD confirmed", invalidation="Below 649",
)
ok("long returns signal", "LONG" in r_long)
ok("long shows entry", "$650.00" in r_long)
ok("long shows RR", "4.0:1" in r_long)

sig = h.get_last_signal()
ok("last_signal set", sig is not None)
ok("signal direction", sig.direction == "LONG")
ok("signal confidence", sig.confidence == "HIGH")
ok("signal entry", sig.entry == 650.0)
ok("signal stop", sig.stop == 649.0)
ok("signal tp1", sig.tp1 == 654.0)
ok("signal tp2", sig.tp2 == 656.0)
ok("signal rr", sig.rr == 4.0)
ok("signal option", sig.option_type == "CALL")
ok("signal strike", sig.strike == 650.0)
ok("signal narrative", sig.narrative == "Bullish breakout confirmed")
ok("signal fired_at", len(sig.fired_at) > 0)
ok("signal session", sig.session == "POWER HOUR")
ok("signal vix", sig.vix_at_signal == 20.0)
ok("signal in history", len(h._signal_history) == 1)

# WAIT signal
r_wait = h.send_signal(
    asset="SPY", signal="WAIT", confidence="MEDIUM", setup_type="REJECTION",
    narrative="Counter-trend", reasoning="Against bias", invalidation="N/A",
    wait_for="Better setup aligned with trend",
)
ok("wait returns reason", "WAIT" in r_wait)
ok("wait shows reason", "Better setup" in r_wait)

sig_w = h.get_last_signal()
ok("wait signal direction", sig_w.direction == "WAIT")
ok("wait signal wait_for", "Better setup" in sig_w.wait_for)
ok("history has 2", len(h._signal_history) == 2)

# SHORT signal
r_short = h.send_signal(
    asset="SPY", signal="SHORT", confidence="MEDIUM", setup_type="STOP_HUNT",
    entry=650.0, stop=651.0, tp1=647.0, rr=3.0,
    option_type="PUT", strike=650.0, expiry_date="Mar 30", dte=1,
    narrative="Bearish stop hunt", reasoning="CVD divergence", invalidation="Above 651",
)
ok("short returns signal", "SHORT" in r_short)

# History cap
h._signal_history = [sig] * 201
h.send_signal(asset="SPY", signal="WAIT", confidence="LOW", setup_type="test",
              narrative="t", reasoning="t", invalidation="t")
ok("history capped at 200", len(h._signal_history) <= 200)

# ── _calc_atr ─────────────────────────────────────────────
print("\n── _calc_atr ──")
bars = [make_candle(t=i*60000, o=100, h=101, l=99, c=100.5, v=1000) for i in range(20)]
atr = _calc_atr(bars)
ok("atr positive", atr > 0)
ok("atr reasonable", 1.0 <= atr <= 3.0, f"got {atr:.2f}")

ok("atr few bars", _calc_atr(bars[:1]) == 1.0)
ok("atr empty", _calc_atr([]) == 1.0)

# Mixed bars
bars_mixed = [
    make_candle(t=0, o=100, h=102, l=99, c=101, v=1000),
    make_candle(t=60000, o=101, h=105, l=100, c=104, v=2000),
    make_candle(t=120000, o=104, h=106, l=102, c=103, v=1500),
]
atr_m = _calc_atr(bars_mixed)
ok("atr mixed reasonable", 2.0 <= atr_m <= 5.0, f"got {atr_m:.2f}")

# ══════════════════════════════════════════════════════════
print(f"\n{'=' * 60}")
print(f"  RESULTS: {PASS} passed, {FAIL} failed, {PASS + FAIL} total")
print(f"{'=' * 60}")

if FAIL > 0:
    sys.exit(1)
