import sys
sys.path.insert(0, ".")
import yfinance as yf
from trading.models import Candle
from trading.levels.builder import build_levels, calc_vwap
from trading.levels.volume_profile import compute_volume_profile
from trading.levels.zones import detect_zones
from datetime import datetime
import pytz, time

ET = pytz.timezone("America/New_York")

def to_candles(df):
    out = []
    for ts, row in df.iterrows():
        try:
            c = Candle(t=int(ts.timestamp()*1000), o=round(float(row["Open"]),2), h=round(float(row["High"]),2), l=round(float(row["Low"]),2), c=round(float(row["Close"]),2), v=float(row["Volume"]))
            if c.c > 0 and c.h >= c.l: out.append(c)
        except: pass
    return out

total_all = 0
for asset in ["SPY","QQQ","AAPL","NVDA","TSLA","MSFT","META","AMZN"]:
    t = yf.Ticker(asset)
    daily = to_candles(t.history(period="2y", interval="1d"))
    c1m_all = to_candles(t.history(period="1d", interval="1m"))
    c5m = to_candles(t.history(period="5d", interval="5m"))
    if not daily or not c1m_all: continue

    c1m = [c for c in c1m_all if 570 <= datetime.fromtimestamp(c.t/1000, tz=ET).hour * 60 + datetime.fromtimestamp(c.t/1000, tz=ET).minute < 660]
    if not c1m: continue

    price = c1m[-1].c
    or_bars = [c for c in c1m if datetime.fromtimestamp(c.t/1000, tz=ET).hour * 60 + datetime.fromtimestamp(c.t/1000, tz=ET).minute < 600]
    or_h = max(c.h for c in or_bars) if or_bars else 0
    or_l = min(c.l for c in or_bars) if or_bars else 0
    vwap = calc_vwap(c1m)
    vp = compute_volume_profile(asset, c1m)
    zones = detect_zones(daily, price)
    levels = build_levels(asset, daily, c1m, c5m, price, vwap, or_h, or_l, True, vp, zones)
    total_all += len(levels)

    above = [l for l in levels if l.price > price]
    below = [l for l in levels if l.price <= price]

    print()
    print("=" * 80)
    print("  {} @ ${:.2f}  |  {} levels  |  {} above  {} below".format(asset, price, len(levels), len(above), len(below)))
    print("=" * 80)

    sorted_levels = sorted(levels, key=lambda x: x.price, reverse=True)
    printed_price = False
    for l in sorted_levels:
        dist = abs(l.price - price)
        pos = "+" if l.price > price else "-"
        conf = " *CONF" if l.confluence_with else ""
        near = " <<<" if dist < 2.0 else ""
        if not printed_price and l.price <= price:
            print("  {:<12} ---- ${:.2f} PRICE ----".format("", price))
            printed_price = True
        print("  {:<12} ${:>8.2f}  {}${:>6.2f}  score={:>2}  {:<11}  {:<8}{}{}".format(
            l.name, l.price, pos, dist, l.score, l.type, l.source, conf, near))
    if not printed_price:
        print("  {:<12} ---- ${:.2f} PRICE ----".format("", price))

    names = [l.name for l in levels]
    sources = sorted(set(l.source for l in levels))
    missing = []
    for req in ["PDH","PDL","PDC","ORH","ORL","VWAP","PWH","PWL","MoH","MoL","52WH","52WL"]:
        if not any(req in n for n in names):
            missing.append(req)
    if vp:
        for req in ["POC","VAH","VAL"]:
            if not any(req in n for n in names):
                missing.append(req)

    print("  Sources: {}".format(" | ".join(sources)))
    if missing:
        print("  MISSING: {}".format(", ".join(missing)))
    else:
        print("  ALL KEY LEVELS PRESENT")

    time.sleep(0.5)

print()
print("=" * 80)
print("  TOTAL: {} levels across 8 assets  |  Avg: {:.0f} per asset".format(total_all, total_all / 8))
print("=" * 80)
