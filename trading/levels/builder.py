import pytz
from datetime import datetime

from ..models import Level, Candle, VolumeProfile
from ..constants import (
    SCORE_52W, SCORE_MONTHLY, SCORE_WEEKLY,
    SCORE_PDH_PDL, SCORE_PDC, SCORE_ORH_ORL,
    SCORE_POC, SCORE_VAH_VAL, SCORE_HVN,
    SCORE_VWAP, SCORE_ROUND_10, SCORE_ROUND_5,
    SCORE_PMH_PML, SCORE_PD_POC, SCORE_PD_VAH_VAL,
)
from ..core.asset_registry import get_config
from .scorer import apply_confluence

ET = pytz.timezone("America/New_York")

# Minimum score threshold for detection (levels scoring below this are filtered)
MIN_LEVEL_SCORE_FOR_DETECTION = 7

# Confluence constants
CONFLUENCE_DISTANCE_PCT = 0.0030  # 0.30%
CONFLUENCE_BONUS = 2
CONFLUENCE_CAP = 12


def apply_volume_multiplier(level: Level, vol_profile: object, prior_day_vp: object) -> int:
    """V3.1 Volume Node Alignment multiplier.
    Maps static levels against the relevant volume profile:
    - PDH/PDL/PMH/PML → prior day's profile
    - ORH/ORL → today's developing profile
    If level sits in HVN → 1.5x, in LVN → 0.5x, else 1.0x.
    """
    # Choose the right profile for this level type
    if level.source in ("PD", "PM"):
        vp = prior_day_vp
    elif level.source == "OR":
        vp = vol_profile
    else:
        return level.score  # no multiplier for 52W, MONTHLY, WEEKLY, VOLUME, VWAP, ZONE

    if not vp:
        return level.score

    alignment = _check_volume_alignment(level.price, vp)
    if alignment == "HVN":
        return max(1, round(level.score * 1.5))
    elif alignment == "LVN":
        return max(1, round(level.score * 0.5))
    return level.score


def _check_volume_alignment(price: float, vp) -> str:
    """Check if a price sits in an HVN, LVN, or neither."""
    # Check HVN — price within 0.1% of any high volume node or POC
    hvn_prices = list(getattr(vp, 'hvn_list', []) or [])
    if hasattr(vp, 'poc') and vp.poc > 0:
        hvn_prices.append(vp.poc)
    for hvn in hvn_prices:
        if hvn > 0 and abs(price - hvn) / price <= 0.001:
            return "HVN"

    # Check LVN — price falls inside a low volume zone
    for lvn in (getattr(vp, 'lvn_zones', []) or []):
        low = lvn.get("low", 0) if isinstance(lvn, dict) else getattr(lvn, 'low', 0)
        high = lvn.get("high", 0) if isinstance(lvn, dict) else getattr(lvn, 'high', 0)
        if low <= price <= high:
            return "LVN"

    return "NEUTRAL"


def build_levels(
    asset: str, daily_bars: list[Candle], c1m_today: list[Candle],
    c5m_recent: list[Candle], current_price: float, vwap: float,
    or_high: float, or_low: float, or_complete: bool,
    vol_profile: object, zones: list, gap_pct: float = 0.0,
    prior_day_vp: VolumeProfile | None = None,
) -> list[Level]:
    cfg = get_config(asset)
    levels = []

    if len(daily_bars) >= 252:
        year = daily_bars[-252:]
        levels.append(Level(name="52WH", price=max(c.h for c in year), score=SCORE_52W, type="resistance", source="52W", confidence="HIGH", description="52-week high — major institutional resistance."))
        levels.append(Level(name="52WL", price=min(c.l for c in year), score=SCORE_52W, type="support", source="52W", confidence="HIGH", description="52-week low — major institutional support."))

    if len(daily_bars) >= 22:
        month = daily_bars[-22:]
        levels.append(Level(name="MoH", price=max(c.h for c in month), score=SCORE_MONTHLY, type="resistance", source="MONTHLY", confidence="HIGH", description="Monthly high — fund manager target."))
        levels.append(Level(name="MoL", price=min(c.l for c in month), score=SCORE_MONTHLY, type="support", source="MONTHLY", confidence="HIGH", description="Monthly low — fund manager support."))

    if len(daily_bars) >= 5:
        week = daily_bars[-5:]
        levels.append(Level(name="PWH", price=max(c.h for c in week), score=SCORE_WEEKLY, type="resistance", source="WEEKLY", confidence="HIGH", description="Previous week high."))
        levels.append(Level(name="PWL", price=min(c.l for c in week), score=SCORE_WEEKLY, type="support", source="WEEKLY", confidence="HIGH", description="Previous week low."))

    if len(daily_bars) >= 2:
        prev = daily_bars[-2]
        levels.append(Level(name="PDH", price=prev.h, score=SCORE_PDH_PDL, type="resistance", source="PD", confidence="HIGH", description=f"Previous day high ${prev.h:.2f} — sellers defended yesterday."))
        levels.append(Level(name="PDL", price=prev.l, score=SCORE_PDH_PDL, type="support", source="PD", confidence="HIGH", description=f"Previous day low ${prev.l:.2f} — buyers defended yesterday."))
        levels.append(Level(name="PDC", price=prev.c, score=SCORE_PDC, type="pivot", source="PD", confidence="HIGH", description=f"Previous day close ${prev.c:.2f} — gap fill level."))

    if or_complete and or_high > 0 and or_low > 0:
        # Boost ORH/ORL to score 8 on gap days (>= 0.5%) so rejection detection can fire
        # On gap days, ORH/ORL are often the only actionable levels since historical levels are far away
        or_score = SCORE_ORH_ORL + 1 if abs(gap_pct) >= 0.5 else SCORE_ORH_ORL
        gap_note = f" (boosted — {gap_pct:+.1f}% gap day)" if abs(gap_pct) >= 0.5 else ""
        levels.append(Level(name="ORH", price=or_high, score=or_score, type="resistance", source="OR", confidence="HIGH", description=f"Opening Range High — break above = bulls won.{gap_note}"))
        levels.append(Level(name="ORL", price=or_low, score=or_score, type="support", source="OR", confidence="HIGH", description=f"Opening Range Low — break below = bears won.{gap_note}"))

    if c5m_recent and len(daily_bars) >= 1:
        pm_bars = _get_premarket_bars(c5m_recent)
        if pm_bars:
            prev_close = daily_bars[-1].c
            if prev_close > 0 and abs(current_price - prev_close) / prev_close >= 0.003:
                levels.append(Level(name="PMH", price=max(c.h for c in pm_bars), score=SCORE_PMH_PML, type="resistance", source="PM", confidence="MEDIUM", description="Pre-market high."))
                levels.append(Level(name="PML", price=min(c.l for c in pm_bars), score=SCORE_PMH_PML, type="support", source="PM", confidence="MEDIUM", description="Pre-market low."))

    if vwap > 0:
        pos = "above" if current_price > vwap else "below"
        levels.append(Level(name="VWAP", price=vwap, score=SCORE_VWAP, type="dynamic", source="VWAP", confidence="HIGH", description=f"VWAP ${vwap:.2f} — price {pos}."))

    # Prior Day Volume Profile — stable institutional levels (V3.1)
    if prior_day_vp:
        if prior_day_vp.poc > 0:
            levels.append(Level(name="pdPOC", price=prior_day_vp.poc, score=SCORE_PD_POC, type="pivot", source="PD_VOLUME", confidence="HIGH", description=f"Prior Day POC ${prior_day_vp.poc:.2f} — settled institutional magnet."))
        if prior_day_vp.vah > 0:
            levels.append(Level(name="pdVAH", price=prior_day_vp.vah, score=SCORE_PD_VAH_VAL, type="resistance", source="PD_VOLUME", confidence="HIGH", description=f"Prior Day VAH ${prior_day_vp.vah:.2f} — settled value area boundary."))
        if prior_day_vp.val > 0:
            levels.append(Level(name="pdVAL", price=prior_day_vp.val, score=SCORE_PD_VAH_VAL, type="support", source="PD_VOLUME", confidence="HIGH", description=f"Prior Day VAL ${prior_day_vp.val:.2f} — settled value area boundary."))

    # Today's developing Volume Profile
    if vol_profile:
        if vol_profile.poc > 0:
            levels.append(Level(name="dPOC", price=vol_profile.poc, score=SCORE_POC, type="pivot", source="VOLUME", confidence="HIGH", description=f"Developing POC ${vol_profile.poc:.2f}."))
        if vol_profile.vah > 0:
            levels.append(Level(name="dVAH", price=vol_profile.vah, score=SCORE_VAH_VAL, type="resistance", source="VOLUME", confidence="HIGH", description=f"Developing VAH ${vol_profile.vah:.2f}."))
        if vol_profile.val > 0:
            levels.append(Level(name="dVAL", price=vol_profile.val, score=SCORE_VAH_VAL, type="support", source="VOLUME", confidence="HIGH", description=f"Developing VAL ${vol_profile.val:.2f}."))
        for i, hvn in enumerate(vol_profile.hvn_list[:3]):
            levels.append(Level(name=f"HVN{i+1}", price=hvn, score=SCORE_HVN, type="pivot", source="VOLUME", confidence="MEDIUM", description=f"High volume node ${hvn:.2f}."))

    for zone in zones:
        levels.append(Level(name=f"ZH_{zone.zone_mid:.0f}", price=zone.zone_high, score=zone.score, type="resistance", source="ZONE", confidence="HIGH", description=f"Zone top ${zone.zone_high:.2f} — tested {zone.test_count}x."))
        levels.append(Level(name=f"ZL_{zone.zone_mid:.0f}", price=zone.zone_low, score=max(zone.score - 1, 1), type="support", source="ZONE", confidence="HIGH", description=f"Zone base ${zone.zone_low:.2f}."))

    # Round numbers removed — they are noise, not institutional levels.
    # The system trades based on PDH/PDL/OR/VWAP/zones/volume profile.
    # Round numbers don't have institutional memory behind them.

    levels = [l for l in levels if l.price > 0]
    if current_price > 0:
        levels = [l for l in levels if abs(l.price - current_price) / current_price <= 0.05 or l.source in ("52W", "PD_VOLUME")]

    # V3.1: Volume Node Alignment — adjust scores for PDH/PDL/ORH/ORL/PMH/PML
    for lvl in levels:
        if lvl.source in ("PD", "PM", "OR"):
            lvl.score = apply_volume_multiplier(lvl, vol_profile, prior_day_vp)

    levels = apply_confluence(levels)
    levels.sort(key=lambda l: l.price, reverse=True)
    return levels


def _get_premarket_bars(c5m: list[Candle]) -> list[Candle]:
    return [c for c in c5m if 240 <= datetime.fromtimestamp(c.t / 1000, tz=ET).hour * 60 + datetime.fromtimestamp(c.t / 1000, tz=ET).minute < 570]


def calc_vwap(candles_today: list[Candle]) -> float:
    if not candles_today:
        return 0.0
    tp_vol = sum(((c.h + c.l + c.c) / 3) * c.v for c in candles_today)
    vol = sum(c.v for c in candles_today)
    return tp_vol / vol if vol > 0 else 0.0


def get_market_open_ts() -> int:
    now = datetime.now(ET)
    start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def filter_today_bars(c1m: list[Candle]) -> list[Candle]:
    """Filter bars to the most recent trading day's market hours.
    Uses the bars' own timestamps (not wall clock) so it works in simulation."""
    if not c1m:
        return []
    # Use the last bar's date as "today" (works for live and sim)
    last_dt = datetime.fromtimestamp(c1m[-1].t / 1000, tz=ET)
    day_open = last_dt.replace(hour=9, minute=30, second=0, microsecond=0)
    start = int(day_open.timestamp() * 1000)
    return [c for c in c1m if c.t >= start]
