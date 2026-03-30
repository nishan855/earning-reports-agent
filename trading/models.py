from dataclasses import dataclass, field
from typing import Literal
from enum import Enum


class Direction(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class PatternType(str, Enum):
    BREAKOUT_RETEST = "BREAKOUT_RETEST"
    REJECTION = "REJECTION"
    STOP_HUNT = "STOP_HUNT"
    FAILED_BREAKOUT = "FAILED_BREAKOUT"


class TrackerStatus(str, Enum):
    WATCHING = "WATCHING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    LOCKED = "LOCKED"


class SessionId(str, Enum):
    CLOSED = "CLOSED"
    PREMARKET = "PREMARKET"
    OR = "OR"
    POWER = "POWER"
    MID = "MID"
    DEAD = "DEAD"
    AFT = "AFT"
    CLOSE = "CLOSE"
    CUTOFF = "CUTOFF"
    AH = "AH"


@dataclass
class Candle:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float

    @property
    def body(self) -> float:
        return abs(self.c - self.o)

    @property
    def upper_wick(self) -> float:
        return self.h - max(self.o, self.c)

    @property
    def lower_wick(self) -> float:
        return min(self.o, self.c) - self.l

    @property
    def is_bullish(self) -> bool:
        return self.c >= self.o

    @property
    def wick_body_ratio(self) -> float:
        if self.body < 0.001:
            return 0.0
        return max(self.upper_wick, self.lower_wick) / self.body


@dataclass
class Level:
    name: str
    price: float
    score: int
    type: str
    source: str
    confidence: str
    description: str = ""
    yesterday_behavior: str = ""
    confluence_with: list = field(default_factory=list)
    tests_today: int = 0
    test_history: list = field(default_factory=list)


@dataclass
class Zone:
    zone_low: float
    zone_high: float
    zone_mid: float
    test_count: int
    last_test_days: int
    avg_rejection: float
    avg_vol_ratio: float
    score: int
    direction: str


@dataclass
class CVDPoint:
    time_et: str
    value: float
    delta: float


@dataclass
class LevelTest:
    time_et: str
    result: str
    candle_high: float
    candle_low: float
    candle_close: float
    volume_ratio: float
    cvd_at_test: float


@dataclass
class LevelState:
    asset: str
    level_name: str
    level_price: float
    level_score: int
    direction: str
    break_candle: Candle = None
    break_time: float = 0.0
    cvd_at_break: float = 0.0
    volume_ratio: float = 0.0
    status: str = "WATCHING"
    candles_watched: list = field(default_factory=list)
    retest_candle: Candle = None
    cvd_at_retest: float = 0.0
    expires_at: float = 0.0
    signal_fired: bool = False


@dataclass
class DayContext:
    asset: str
    day_type: str = "UNKNOWN"
    bias: str = "NEUTRAL"
    bias_locked: bool = False
    gap_pct: float = 0.0
    gap_type: str = "FLAT"
    gap_filled: bool = False
    relative_str: float = 0.0
    or_high: float = 0.0
    or_low: float = 0.0
    or_complete: bool = False


@dataclass
class Signal:
    asset: str
    direction: str
    confidence: str
    pattern: str
    level_name: str
    level_price: float
    confidence_pct: int = 0
    entry: float = 0.0
    stop: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    rr: float = 0.0
    option_type: str = ""
    strike: float = 0.0
    expiry_date: str = ""
    dte: int = 0
    size: str = "FULL"
    est_premium_lo: float = 0.0
    est_premium_hi: float = 0.0
    breakeven: float = 0.0
    instrument: str = ""
    narrative: str = ""
    reasoning: str = ""
    invalidation: str = ""
    warnings: str = ""
    wait_for: str = ""
    fired_at: str = ""
    session: str = ""
    vix_at_signal: float = 0.0


@dataclass
class Session:
    id: SessionId
    label: str
    quality: int
    color: str
    min_remaining: int


@dataclass
class VolumeProfile:
    asset: str
    poc: float
    vah: float
    val: float
    hvn_list: list = field(default_factory=list)
    lvn_zones: list = field(default_factory=list)
    computed_at: str = ""


@dataclass
class AssetState:
    asset: str
    live_price: float = 0.0
    prev_price: float = 0.0
    daily_change: float = 0.0
    cvd: float = 0.0
    cvd_bias: str = "NEUTRAL"
    vix: float = 0.0
    atr: float = 0.0
    vwap: float = 0.0
    day_context: DayContext = None
    active_signal: Signal = None
    signals_today: int = 0
    last_signal_time: float = 0.0
    is_investigating: bool = False


@dataclass
class DataHealth:
    asset: str
    status: str = "HEALTHY"       # HEALTHY | DEGRADED | STALE
    last_tick_at: float = 0.0
    longest_gap_sec: float = 0.0
    bars_backfilled: int = 0
    cvd_drift_pct: float = 0.0
    ws_disconnects: int = 0
    last_validated_at: float = 0.0
