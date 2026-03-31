import pytz
from datetime import datetime
from ..models import Candle, VolumeProfile
from ..core.asset_registry import get_config

ET = pytz.timezone("America/New_York")


def compute_volume_profile(asset: str, candles_today: list[Candle], atr: float = 0.0) -> VolumeProfile | None:
    if len(candles_today) < 5:
        return None

    cfg = get_config(asset)
    # ATR-adaptive bucket: widen on volatile days, keep minimum granularity on calm days
    base_bucket = cfg["price_bucket"]
    bucket_size = max(atr * 0.05, base_bucket) if atr > 0 else base_bucket

    vol_map: dict[float, float] = {}
    for c in candles_today:
        if c.h <= c.l:
            continue
        bar_range = c.h - c.l
        if bar_range < bucket_size:
            bucket = _round_to_bucket(c.c, bucket_size)
            vol_map[bucket] = vol_map.get(bucket, 0) + c.v
            continue
        price = c.l
        num_buckets = max(1, int(bar_range / bucket_size))
        vol_per_bucket = c.v / num_buckets
        while price <= c.h:
            bucket = _round_to_bucket(price, bucket_size)
            vol_map[bucket] = vol_map.get(bucket, 0) + vol_per_bucket
            price += bucket_size

    if not vol_map:
        return None

    poc = max(vol_map, key=vol_map.get)
    total_vol = sum(vol_map.values())
    target_vol = total_vol * 0.70

    sorted_buckets = sorted(vol_map.items(), key=lambda x: x[1], reverse=True)
    accumulated = 0.0
    value_area_prices = []
    for price, vol in sorted_buckets:
        accumulated += vol
        value_area_prices.append(price)
        if accumulated >= target_vol:
            break

    vah = max(value_area_prices) if value_area_prices else poc
    val = min(value_area_prices) if value_area_prices else poc

    avg_vol = total_vol / len(vol_map) if vol_map else 0
    hvn_threshold = avg_vol * 1.5
    hvn_list = sorted(
        [p for p, v in vol_map.items() if v > hvn_threshold and p != poc],
        key=lambda p: vol_map[p], reverse=True
    )[:5]

    lvn_zones = _find_lvn_zones(vol_map, bucket_size, avg_vol)
    now_et = datetime.now(ET).strftime("%H:%M")

    return VolumeProfile(
        asset=asset, poc=poc, vah=vah, val=val,
        hvn_list=hvn_list, lvn_zones=lvn_zones, computed_at=now_et,
    )


def compute_prior_day_profile(asset: str, all_1m_bars: list[Candle], atr: float = 0.0) -> VolumeProfile | None:
    """Compute volume profile from yesterday's market hours bars (9:30-16:00)."""
    if not all_1m_bars:
        return None

    # Find the last bar's date, then get the previous trading day's bars
    last_dt = datetime.fromtimestamp(all_1m_bars[-1].t / 1000, tz=ET)
    today_open = last_dt.replace(hour=9, minute=30, second=0, microsecond=0)
    today_start_ms = int(today_open.timestamp() * 1000)

    # Get all bars before today
    prior_bars = [c for c in all_1m_bars if c.t < today_start_ms]
    if len(prior_bars) < 30:
        return None

    # Find the most recent prior day's bars (last trading day before today)
    last_prior_dt = datetime.fromtimestamp(prior_bars[-1].t / 1000, tz=ET)
    prior_day_open = last_prior_dt.replace(hour=9, minute=30, second=0, microsecond=0)
    prior_day_start_ms = int(prior_day_open.timestamp() * 1000)
    prior_day_close = last_prior_dt.replace(hour=16, minute=0, second=0, microsecond=0)
    prior_day_end_ms = int(prior_day_close.timestamp() * 1000)

    prior_day_bars = [c for c in prior_bars if prior_day_start_ms <= c.t <= prior_day_end_ms]
    if len(prior_day_bars) < 30:
        return None

    return compute_volume_profile(asset, prior_day_bars, atr=atr)


def _round_to_bucket(price: float, bucket_size: float) -> float:
    return round(round(price / bucket_size) * bucket_size, 4)


def _find_lvn_zones(vol_map: dict, bucket_size: float, avg_vol: float) -> list[dict]:
    if not vol_map:
        return []
    low_threshold = avg_vol * 0.3
    prices = sorted(vol_map.keys())
    zones = []
    zone_start = None
    zone_end = None
    for price in prices:
        vol = vol_map[price]
        if vol < low_threshold:
            if zone_start is None:
                zone_start = price
            zone_end = price
        else:
            if zone_start is not None:
                zone_size = zone_end - zone_start
                if zone_size >= bucket_size * 3:
                    zones.append({"low": zone_start, "high": zone_end, "size": zone_size})
                zone_start = None
    if zone_start is not None and zone_end is not None:
        zone_size = zone_end - zone_start
        if zone_size >= bucket_size * 3:
            zones.append({"low": zone_start, "high": zone_end, "size": zone_size})
    return zones
