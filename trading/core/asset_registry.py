from ..constants import ASSETS as ASSET_SYMBOLS

ASSET_CONFIG = {
    "SPY": {
        "name": "S&P 500 ETF",
        "type": "etf",
        "benchmark": None,
        "price_bucket": 0.25,
        "round_interval_major": 10,
        "round_interval_minor": 5,
        "options_multiplier": 100,
        "daily_expiry": True,
    },
    "QQQ": {
        "name": "Nasdaq 100 ETF",
        "type": "etf",
        "benchmark": None,
        "price_bucket": 0.25,
        "round_interval_major": 10,
        "round_interval_minor": 5,
        "options_multiplier": 100,
        "daily_expiry": True,
    },
    "AAPL": {
        "name": "Apple Inc",
        "type": "stock",
        "benchmark": "QQQ",
        "price_bucket": 0.10,
        "round_interval_major": 10,
        "round_interval_minor": 5,
        "options_multiplier": 100,
        "daily_expiry": False,
    },
    "NVDA": {
        "name": "NVIDIA Corp",
        "type": "stock",
        "benchmark": "QQQ",
        "price_bucket": 0.25,
        "round_interval_major": 50,
        "round_interval_minor": 25,
        "options_multiplier": 100,
        "daily_expiry": False,
    },
    "TSLA": {
        "name": "Tesla Inc",
        "type": "stock",
        "benchmark": "QQQ",
        "price_bucket": 0.10,
        "round_interval_major": 10,
        "round_interval_minor": 5,
        "options_multiplier": 100,
        "daily_expiry": False,
    },
    "MSFT": {
        "name": "Microsoft Corp",
        "type": "stock",
        "benchmark": "QQQ",
        "price_bucket": 0.10,
        "round_interval_major": 10,
        "round_interval_minor": 5,
        "options_multiplier": 100,
        "daily_expiry": False,
    },
    "META": {
        "name": "Meta Platforms",
        "type": "stock",
        "benchmark": "QQQ",
        "price_bucket": 0.10,
        "round_interval_major": 10,
        "round_interval_minor": 5,
        "options_multiplier": 100,
        "daily_expiry": False,
    },
    "AMZN": {
        "name": "Amazon Inc",
        "type": "stock",
        "benchmark": "QQQ",
        "price_bucket": 0.10,
        "round_interval_major": 10,
        "round_interval_minor": 5,
        "options_multiplier": 100,
        "daily_expiry": False,
    },
    "JPM": {
        "name": "JPMorgan Chase",
        "type": "stock",
        "benchmark": "SPY",
        "price_bucket": 0.10,
        "round_interval_major": 10,
        "round_interval_minor": 5,
        "options_multiplier": 100,
        "daily_expiry": False,
    },
    "XLE": {
        "name": "Energy Select Sector ETF",
        "type": "etf",
        "benchmark": "SPY",
        "price_bucket": 0.10,
        "round_interval_major": 5,
        "round_interval_minor": 1,
        "options_multiplier": 100,
        "daily_expiry": False,
    },
    "GLD": {
        "name": "SPDR Gold Shares ETF",
        "type": "etf",
        "benchmark": None,
        "price_bucket": 0.25,
        "round_interval_major": 10,
        "round_interval_minor": 5,
        "options_multiplier": 100,
        "daily_expiry": False,
    },
    "BA": {
        "name": "Boeing Co",
        "type": "stock",
        "benchmark": "SPY",
        "price_bucket": 0.10,
        "round_interval_major": 10,
        "round_interval_minor": 5,
        "options_multiplier": 100,
        "daily_expiry": False,
    },
}


def get_config(asset: str) -> dict:
    return ASSET_CONFIG[asset]


def get_benchmark(asset: str) -> str | None:
    return ASSET_CONFIG[asset]["benchmark"]


def has_daily_expiry(asset: str) -> bool:
    return ASSET_CONFIG[asset]["daily_expiry"]


def all_assets() -> list[str]:
    return list(ASSET_CONFIG.keys())
