import pytz
from datetime import datetime

from ..models import Level, Candle, VolumeProfile
from ..constants import (
    SCORE_52W, SCORE_MONTHLY, SCORE_WEEKLY,
    SCORE_PDH_PDL, SCORE_ORH_ORL,
    SCORE_PMH_PML, SCORE_PD_POC, SCORE_PD_VAH_VAL,
)
from ..core.asset_registry import get_config
from .scorer import apply_confluence
from ..context.sim_clock import now_et

ET = pytz.timezone("America/New_York")

MIN_LEVEL_SCORE_FOR_DETECTION = 7


# ── Volume Node Alignment (V3.1) ────────────────────────

def apply_volume_multiplier(level: Level, vol_profile: object, prior_day_vp: object) -> int:
    """V3.1 Volume Node Alignment multiplier.
    PDH/PDL/PMH/PML → prior day's profile. ORH/ORL → today's profile.
    HVN → 1.5x, LVN → 0.5x, else 1.0x."""
    if level.source in ("PD", "PM"):
        vp = prior_day_vp
    elif level.source == "OR":
        vp = vol_profile
    else:
        return level.score
    if not vp:
        return level.score
    alignment = _check_volume_alignment(level.price, vp)
    if alignment == "HVN":
        return max(1, round(level.score * 1.5))
    elif alignment == "LVN":
        return max(1, round(level.score * 0.5))
    return level.score


def _check_volume_alignment(price: float, vp) -> str:
    hvn_prices = list(getattr(vp, 'hvn_list', []) or [])
    if hasattr(vp, 'poc') and vp.poc > 0:
        hvn_prices.append(vp.poc)
    for hvn in hvn_prices:
        if hvn > 0 and abs(price - hvn) / price <= 0.001:
            return "HVN"
    for lvn in (getattr(vp, 'lvn_zones', []) or []):
        low = lvn.get("low", 0) if isinstance(lvn, dict) else getattr(lvn, 'low', 0)
        high = lvn.get("high", 0) if isinstance(lvn, dict) else getattr(lvn, 'high', 0)
        if low <= price <= high:
            return "LVN"
    return "NEUTRAL"


# ── Swing Detection (V3.1 Section 2) ────────────────────

def find_swing_highs(candles: list, left_bars: int = 5, right_bars: int = 5) -> list:
    """Structural swing highs — clear inverted U shape.
    N candles with lower highs on BOTH sides of peak."""
    swings = []
    for i in range(left_bars, len(candles) - right_bars):
        pivot = candles[i].h
        left_ok = all(candles[j].h < pivot for j in range(i - left_bars, i))
        right_ok = all(candles[j].h < pivot for j in range(i + 1, i + right_bars + 1))
        if left_ok and right_ok:
            swings.append({"price": pivot, "timestamp": candles[i].t, "bars_old": len(candles) - 1 - i})
    return swings


def find_swing_lows(candles: list, left_bars: int = 5, right_bars: int = 5) -> list:
    """Structural swing lows — clear U shape.
    N candles with higher lows on BOTH sides of trough."""
    swings = []
    for i in range(left_bars, len(candles) - right_bars):
        pivot = candles[i].l
        left_ok = all(candles[j].l > pivot for j in range(i - left_bars, i))
        right_ok = all(candles[j].l > pivot for j in range(i + 1, i + right_bars + 1))
        if left_ok and right_ok:
            swings.append({"price": pivot, "timestamp": candles[i].t, "bars_old": len(candles) - 1 - i})
    return swings


def score_swing_level(bars_old: int, is_daily: bool) -> int:
    """Score a swing level based on age."""
    if is_daily:
        if bars_old <= 5: return 9
        elif bars_old <= 15: return 8
        elif bars_old <= 45: return 7  # extended from 30 — 45 day swings still valid
        else: return 6
    else:
        if bars_old <= 12: return 7  # extended from 8 — 3 hours still relevant
        elif bars_old <= 24: return 6
        else: return 5


# ── ORH/ORL Time Decay (V3.1 Section 4) ─────────────────

def get_orh_orl_score(formed_at_ms: int, tests_today: int, current_ms: int, base_score: int = 8) -> int:
    """ORH/ORL score decays with time and tests. Returns 0 if should be filtered."""
    hours_old = (current_ms - formed_at_ms) / 3_600_000
    if hours_old > 5.25:
        return 0
    elif hours_old > 4.0:
        base_score -= 2
    elif hours_old > 2.5:
        base_score -= 1
    if tests_today >= 2:
        base_score -= 2
    elif tests_today == 1:
        base_score -= 1
    return max(0, base_score)


# ── Developing Level Time Gating (V3.1 Section 5) ───────

def get_developing_level_score(level_type: str, current_hour: float) -> int:
    """Developing levels gain reliability as session progresses."""
    if level_type == "dPOC":
        if current_hour < 11.0: return 5
        elif current_hour < 13.0: return 6
        else: return 7
    elif level_type in ("dVAH", "dVAL"):
        if current_hour < 11.0: return 4
        elif current_hour < 13.0: return 5
        else: return 6
    return 4


# ── Main Level Builder ───────────────────────────────────

def build_levels(
    asset: str, daily_bars: list[Candle], c1m_today: list[Candle],
    c5m_recent: list[Candle], current_price: float, vwap: float,
    or_high: float, or_low: float, or_complete: bool,
    vol_profile: object, zones: list, gap_pct: float = 0.0,
    prior_day_vp: VolumeProfile | None = None,
    or_lock_ts: int = 0, c15m_recent: list = None,
) -> list[Level]:
    cfg = get_config(asset)
    levels = []
    now = now_et()
    current_hour = now.hour + now.minute / 60.0
    current_ms = int(now.timestamp() * 1000)

    # 52-week
    if len(daily_bars) >= 252:
        year = daily_bars[-252:]
        levels.append(Level(name="52WH", price=max(c.h for c in year), score=SCORE_52W, type="resistance", source="52W", confidence="HIGH", description="52-week high — major institutional resistance."))
        levels.append(Level(name="52WL", price=min(c.l for c in year), score=SCORE_52W, type="support", source="52W", confidence="HIGH", description="52-week low — major institutional support."))

    # Monthly
    if len(daily_bars) >= 22:
        month = daily_bars[-22:]
        levels.append(Level(name="MoH", price=max(c.h for c in month), score=SCORE_MONTHLY, type="resistance", source="MONTHLY", confidence="HIGH", description="Monthly high — fund manager target."))
        levels.append(Level(name="MoL", price=min(c.l for c in month), score=SCORE_MONTHLY, type="support", source="MONTHLY", confidence="HIGH", description="Monthly low — fund manager support."))

    # Weekly
    if len(daily_bars) >= 5:
        week = daily_bars[-5:]
        levels.append(Level(name="PWH", price=max(c.h for c in week), score=SCORE_WEEKLY, type="resistance", source="WEEKLY", confidence="HIGH", description="Previous week high."))
        levels.append(Level(name="PWL", price=min(c.l for c in week), score=SCORE_WEEKLY, type="support", source="WEEKLY", confidence="HIGH", description="Previous week low."))

    # PDH/PDL (NO PDC — removed in V3.1)
    if len(daily_bars) >= 2:
        prev = daily_bars[-2]
        levels.append(Level(name="PDH", price=prev.h, score=SCORE_PDH_PDL, type="resistance", source="PD", confidence="HIGH", description=f"Previous day high ${prev.h:.2f} — sellers defended yesterday."))
        levels.append(Level(name="PDL", price=prev.l, score=SCORE_PDH_PDL, type="support", source="PD", confidence="HIGH", description=f"Previous day low ${prev.l:.2f} — buyers defended yesterday."))

    # ORH/ORL with time decay (Section 4)
    if or_complete and or_high > 0 and or_low > 0:
        or_base = SCORE_ORH_ORL + 1 if abs(gap_pct) >= 0.5 else SCORE_ORH_ORL
        formed_ts = or_lock_ts if or_lock_ts > 0 else current_ms
        orh_score = get_orh_orl_score(formed_ts, 0, current_ms, or_base)
        orl_score = get_orh_orl_score(formed_ts, 0, current_ms, or_base)
        gap_note = f" (gap day {gap_pct:+.1f}%)" if abs(gap_pct) >= 0.5 else ""
        if orh_score > 0:
            levels.append(Level(name="ORH", price=or_high, score=orh_score, type="resistance", source="OR", confidence="HIGH", description=f"Opening Range High{gap_note}"))
        if orl_score > 0:
            levels.append(Level(name="ORL", price=or_low, score=orl_score, type="support", source="OR", confidence="HIGH", description=f"Opening Range Low{gap_note}"))

    # PMH/PML — volume gated (Section 3): only if PM volume > 10% avg daily
    if c5m_recent and len(daily_bars) >= 20:
        pm_bars = _get_premarket_bars(c5m_recent)
        if pm_bars:
            pm_volume = sum(c.v for c in pm_bars)
            avg_daily_vol = sum(c.v for c in daily_bars[-20:]) / 20
            if avg_daily_vol > 0 and pm_volume > avg_daily_vol * 0.10:
                levels.append(Level(name="PMH", price=max(c.h for c in pm_bars), score=SCORE_PMH_PML, type="resistance", source="PM", confidence="MEDIUM", description="Pre-market high (volume confirmed)."))
                levels.append(Level(name="PML", price=min(c.l for c in pm_bars), score=SCORE_PMH_PML, type="support", source="PM", confidence="MEDIUM", description="Pre-market low (volume confirmed)."))

    # VWAP removed as level — stays as bias filter only (Section 3)
    # vwap float still passed to day_context for bias computation

    # Prior Day Volume Profile (V3.1)
    if prior_day_vp:
        if prior_day_vp.poc > 0:
            levels.append(Level(name="pdPOC", price=prior_day_vp.poc, score=SCORE_PD_POC, type="pivot", source="PD_VOLUME", confidence="HIGH", description=f"Prior Day POC ${prior_day_vp.poc:.2f} — settled institutional magnet."))
        if prior_day_vp.vah > 0:
            levels.append(Level(name="pdVAH", price=prior_day_vp.vah, score=SCORE_PD_VAH_VAL, type="resistance", source="PD_VOLUME", confidence="HIGH", description=f"Prior Day VAH ${prior_day_vp.vah:.2f} — settled value area boundary."))
        if prior_day_vp.val > 0:
            levels.append(Level(name="pdVAL", price=prior_day_vp.val, score=SCORE_PD_VAH_VAL, type="support", source="PD_VOLUME", confidence="HIGH", description=f"Prior Day VAL ${prior_day_vp.val:.2f} — settled value area boundary."))

    # Developing VP — time-gated scores (Section 5)
    if vol_profile:
        dpoc_score = get_developing_level_score("dPOC", current_hour)
        if vol_profile.poc > 0:
            levels.append(Level(name="dPOC", price=vol_profile.poc, score=dpoc_score, type="pivot", source="VOLUME", confidence="HIGH", description=f"Developing POC ${vol_profile.poc:.2f}."))
        dvah_score = get_developing_level_score("dVAH", current_hour)
        if vol_profile.vah > 0:
            levels.append(Level(name="dVAH", price=vol_profile.vah, score=dvah_score, type="resistance", source="VOLUME", confidence="HIGH", description=f"Developing VAH ${vol_profile.vah:.2f}."))
        dval_score = get_developing_level_score("dVAL", current_hour)
        if vol_profile.val > 0:
            levels.append(Level(name="dVAL", price=vol_profile.val, score=dval_score, type="support", source="VOLUME", confidence="HIGH", description=f"Developing VAL ${vol_profile.val:.2f}."))
        # HVN removed as standalone — only affects confluence scoring (Section 3)

    # Zones
    for zone in zones:
        levels.append(Level(name=f"ZH_{zone.zone_mid:.0f}", price=zone.zone_high, score=zone.score, type="resistance", source="ZONE", confidence="HIGH", description=f"Zone top ${zone.zone_high:.2f} — tested {zone.test_count}x."))
        levels.append(Level(name=f"ZL_{zone.zone_mid:.0f}", price=zone.zone_low, score=max(zone.score - 1, 1), type="support", source="ZONE", confidence="HIGH", description=f"Zone base ${zone.zone_low:.2f}."))

    # Swing highs/lows — daily (nearest 2 above + 2 below current price)
    if len(daily_bars) >= 10 and current_price > 0:
        all_sh = [sh for sh in find_swing_highs(daily_bars, 3, 3) if score_swing_level(sh["bars_old"], True) >= MIN_LEVEL_SCORE_FOR_DETECTION]
        all_sl = [sl for sl in find_swing_lows(daily_bars, 3, 3) if score_swing_level(sl["bars_old"], True) >= MIN_LEVEL_SCORE_FOR_DETECTION]
        # 2 nearest above price + 2 nearest below price
        above = sorted([s for s in all_sh if s["price"] > current_price], key=lambda s: s["price"])[:2]
        below = sorted([s for s in all_sl if s["price"] < current_price], key=lambda s: -s["price"])[:2]
        for sh in above:
            score = score_swing_level(sh["bars_old"], is_daily=True)
            levels.append(Level(name=f"SwH_{sh['price']:.2f}", price=sh["price"], score=score, type="resistance", source="SWING_HIGH", confidence="HIGH", description=f"Structural swing high ({sh['bars_old']} days old)"))
        for sl in below:
            score = score_swing_level(sl["bars_old"], is_daily=True)
            levels.append(Level(name=f"SwL_{sl['price']:.2f}", price=sl["price"], score=score, type="support", source="SWING_LOW", confidence="HIGH", description=f"Structural swing low ({sl['bars_old']} days old)"))

    # Swing highs/lows — 15m (nearest 1 above + 1 below)
    if c15m_recent and current_price > 0:
        bars_15m_today = [b for b in c15m_recent if _is_today(b.t)]
        if len(bars_15m_today) >= 10:
            all_sh_15 = [sh for sh in find_swing_highs(bars_15m_today, 3, 3) if score_swing_level(sh["bars_old"], False) >= MIN_LEVEL_SCORE_FOR_DETECTION]
            all_sl_15 = [sl for sl in find_swing_lows(bars_15m_today, 3, 3) if score_swing_level(sl["bars_old"], False) >= MIN_LEVEL_SCORE_FOR_DETECTION]
            above_15 = sorted([s for s in all_sh_15 if s["price"] > current_price], key=lambda s: s["price"])[:1]
            below_15 = sorted([s for s in all_sl_15 if s["price"] < current_price], key=lambda s: -s["price"])[:1]
            for sh in above_15:
                score = score_swing_level(sl["bars_old"], is_daily=False)
                if score >= MIN_LEVEL_SCORE_FOR_DETECTION:
                    levels.append(Level(name=f"15mSwL_{sl['price']:.2f}", price=sl["price"], score=score, type="support", source="SWING_LOW", confidence="MEDIUM", description=f"15m swing low ({sl['bars_old']} bars ago)"))

    # Filter — ATR-based distance (3× ATR) instead of flat 5%
    levels = [l for l in levels if l.price > 0]
    if current_price > 0 and len(daily_bars) >= 15:
        trs = [max(daily_bars[i].h - daily_bars[i].l, abs(daily_bars[i].h - daily_bars[i-1].c), abs(daily_bars[i].l - daily_bars[i-1].c)) for i in range(-14, 0)]
        daily_atr = sum(trs) / len(trs) if trs else current_price * 0.02
        max_dist = daily_atr * 3
        levels = [l for l in levels if abs(l.price - current_price) <= max_dist or l.source in ("52W", "PD_VOLUME")]
    elif current_price > 0:
        levels = [l for l in levels if abs(l.price - current_price) / current_price <= 0.05 or l.source in ("52W", "PD_VOLUME")]

    # Volume Node Alignment (V3.1)
    for lvl in levels:
        if lvl.source in ("PD", "PM", "OR"):
            lvl.score = apply_volume_multiplier(lvl, vol_profile, prior_day_vp)

    levels = apply_confluence(levels)
    levels.sort(key=lambda l: l.price, reverse=True)
    return levels


def _is_today(ts_ms: int) -> bool:
    now = now_et()
    bar_dt = datetime.fromtimestamp(ts_ms / 1000, tz=ET)
    return bar_dt.date() == now.date()


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
    if not c1m:
        return []
    last_dt = datetime.fromtimestamp(c1m[-1].t / 1000, tz=ET)
    day_open = last_dt.replace(hour=9, minute=30, second=0, microsecond=0)
    start = int(day_open.timestamp() * 1000)
    return [c for c in c1m if c.t >= start]
