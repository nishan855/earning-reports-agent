import sys, time
sys.path.insert(0, ".")
import yfinance as yf
from trading.models import Candle
from trading.levels.builder import build_levels, calc_vwap, filter_today_bars
from trading.levels.volume_profile import compute_volume_profile
from trading.levels.zones import detect_zones
from trading.context.day_context import assess_day_context
from trading.context.options_context import get_options_env, get_strike, get_expiry, estimate_premium
from trading.detection.breakout import detect_breakout
from trading.detection.rejection import detect_rejection
from datetime import datetime
import pytz
ET = pytz.timezone("America/New_York")

def to_candles(df):
    out = []
    for ts, row in df.iterrows():
        try:
            c = Candle(t=int(ts.timestamp()*1000), o=round(float(row["Open"]),2), h=round(float(row["High"]),2), l=round(float(row["Low"]),2), c=round(float(row["Close"]),2), v=float(row["Volume"]))
            if c.c > 0 and c.h >= c.l: out.append(c)
        except: pass
    return out

print("=" * 70)
print("FRIDAY MARCH 28, 2026 — COMPLETE MARKET DATA")
print("=" * 70)

vix = 31.0
try:
    vh = yf.Ticker("^VIX").history(period="1d", interval="1m")
    if not vh.empty: vix = float(vh["Close"].iloc[-1])
except: pass
env = get_options_env(vix)
print(f"\nVIX: {vix:.2f} — {env['label']} | Size: {env['size']} | Instrument: {env['instrument']}")

for asset in ["SPY","QQQ","AAPL","NVDA","TSLA","MSFT","META","AMZN"]:
    time.sleep(0.5)
    try:
        t = yf.Ticker(asset)
        c1m = to_candles(t.history(period="1d", interval="1m"))
        c5m = to_candles(t.history(period="5d", interval="5m"))
        c15m = to_candles(t.history(period="30d", interval="15m"))
        daily = to_candles(t.history(period="2y", interval="1d"))
        if not c1m or not daily:
            print(f"\n{asset}: NO DATA"); continue

        price = c1m[-1].c
        open_p = c1m[0].o
        day_chg = (price - open_p) / open_p * 100
        day_high = max(c.h for c in c1m)
        day_low = min(c.l for c in c1m)
        day_range = day_high - day_low
        total_vol = sum(c.v for c in c1m)

        prev = daily[-2] if len(daily) >= 2 else None
        pdh = prev.h if prev else 0
        pdl = prev.l if prev else 0
        pdc = prev.c if prev else 0
        gap = ((open_p - pdc) / pdc * 100) if pdc > 0 else 0

        today = filter_today_bars(c1m)
        vwap_val = calc_vwap(today) if today else 0

        cum_cvd = 0.0
        for c in c1m:
            rng = c.h - c.l
            if rng > 0:
                ratio = (c.c - c.l) / rng
                cum_cvd += c.v * (ratio - 0.5) * 2
        cvd_bias = "BUYERS" if cum_cvd > total_vol * 0.005 else "SELLERS" if cum_cvd < -total_vol * 0.005 else "NEUTRAL"

        zones = detect_zones(daily, price)
        or_bars = today[:30]
        or_h = max((c.h for c in or_bars), default=0)
        or_l = min((c.l for c in or_bars), default=0)
        vp = compute_volume_profile(asset, today) if len(today) >= 5 else None
        levels = build_levels(asset, daily, today, c5m, price, vwap_val, or_h, or_l, True, vp, zones)
        dc = assess_day_context(asset, daily, c15m, today, or_h, or_l, price)

        avg_v5 = sum(c.v for c in c5m[-20:]) / 20 if len(c5m) >= 20 else 1
        breakouts, rejections = 0, 0
        last_bo, last_rej = None, None
        for i in range(1, len(c5m)):
            candle, prev_c = c5m[i], c5m[i-1]
            cvd_ch = candle.v * 0.6 * (1 if candle.c > candle.o else -1)
            for lvl in levels:
                if lvl.score < 6: continue
                is_bo, bo_dir = detect_breakout(candle, prev_c, lvl, avg_v5, 0, cvd_ch)
                if is_bo: breakouts += 1; last_bo = (lvl.name, lvl.price, bo_dir, candle.v / avg_v5 if avg_v5 > 0 else 0)
                is_rej, rej_dir, strength = detect_rejection(candle, lvl, avg_v5, 0, cvd_ch)
                if is_rej: rejections += 1; last_rej = (lvl.name, lvl.price, rej_dir, strength)

        strike = get_strike(asset, price, "BULLISH", vix)
        dte, exp = get_expiry(asset, 10.5)
        prem_lo, prem_hi = estimate_premium(price, vix)

        sep = "-" * 70
        print(f"\n{sep}")
        print(f"  {asset}")
        print(sep)
        print(f"  PRICE")
        print(f"    Open:  ${open_p:.2f}")
        print(f"    High:  ${day_high:.2f}")
        print(f"    Low:   ${day_low:.2f}")
        print(f"    Close: ${price:.2f}  ({day_chg:+.2f}%)")
        print(f"    Range: ${day_range:.2f}")
        print(f"    Volume: {total_vol:,.0f}")
        print(f"  REFERENCE LEVELS")
        print(f"    PDH: ${pdh:.2f}  PDL: ${pdl:.2f}  PDC: ${pdc:.2f}")
        gap_label = "GAP UP" if gap > 0.3 else "GAP DOWN" if gap < -0.3 else "FLAT"
        print(f"    Gap: {gap:+.2f}% ({gap_label})")
        vwap_pos = "above" if price > vwap_val else "below"
        print(f"    VWAP: ${vwap_val:.2f}  (price {vwap_pos})")
        print(f"    OR: ${or_h:.2f} / ${or_l:.2f}  (range ${or_h - or_l:.2f})")
        print(f"  DAY CHARACTER")
        print(f"    Type: {dc.day_type}  Bias: {dc.bias}")
        print(f"    CVD: {cum_cvd:+,.0f}  ({cvd_bias})")
        print(f"  LEVELS ({len(levels)} total)")
        for lvl in levels[:6]:
            conf = ""
            if lvl.confluence_with:
                conf = " *CONF(" + ",".join(lvl.confluence_with) + ")"
            print(f"    {lvl.name:8} ${lvl.price:>8.2f}  score={lvl.score:>2}{conf}")
        if len(levels) > 6:
            print(f"    ... +{len(levels)-6} more")
        print(f"  ZONES: {len(zones)}")
        for z in zones[:3]:
            print(f"    {z.direction:10} ${z.zone_low:.2f} - ${z.zone_high:.2f}  tests={z.test_count}  score={z.score}")
        if vp:
            print(f"  VOLUME PROFILE")
            print(f"    POC: ${vp.poc:.2f}  VAH: ${vp.vah:.2f}  VAL: ${vp.val:.2f}")
            hvn_str = ", ".join(f"${h:.2f}" for h in vp.hvn_list[:3])
            print(f"    HVN: {hvn_str}")
        print(f"  PATTERNS (5m scan)")
        print(f"    Breakouts: {breakouts}  Rejections: {rejections}")
        if last_bo:
            print(f"    Last BO: {last_bo[2]} at {last_bo[0]} ${last_bo[1]:.2f} vol={last_bo[3]:.1f}x")
        if last_rej:
            print(f"    Last REJ: {last_rej[2]} at {last_rej[0]} ${last_rej[1]:.2f} ({last_rej[3]})")
        print(f"  OPTIONS")
        print(f"    ATM: ${strike:.2f}  DTE: {dte} ({exp})  Premium: ${prem_lo:.2f} - ${prem_hi:.2f}")

    except Exception as e:
        print(f"\n{asset}: ERROR - {e}")

print(f"\n{'=' * 70}")
print("END OF FRIDAY DATA")
print("=" * 70)
