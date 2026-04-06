# Assets
ASSETS = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA", "MSFT", "META", "AMZN", "JPM", "XLE", "GLD", "BA"]

# Market hours ET
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MIN = 30
OR_LOCK_HOUR = 10
OR_LOCK_MIN = 0
OR_DURATION_MIN = 30
CUTOFF_HOUR = 15
CUTOFF_MIN = 15
MARKET_CLOSE_HOUR = 16

# Signal detection
PROXIMITY_PCT = 0.005
CONFLUENCE_DIST_PCT = 0.003
MIN_LEVEL_SCORE = 7
MAX_RETEST_CANDLES = 15
RETEST_PROXIMITY = 0.003

# Volume thresholds
BREAKOUT_VOL_MIN = 1.3
REJECTION_VOL_MIN = 1.2
RETEST_VOL_MIN = 0.8
STOP_HUNT_VOL_MIN = 1.0

# Timeouts
RETEST_TIMEOUT_SEC = 900
STOPHUNT_CONFIRM_SEC = 90
REJECTION_CONFIRM_SEC = 120

# Cooldowns
GLOBAL_PAUSE_SEC = 90
ASSET_COOLDOWN_SEC = 300
LEVEL_LOCK_DAILY = True

# Risk
MIN_RR = 1.5
MAX_SIGNALS_PER_ASSET = 3

# VIX thresholds
VIX_HARD_BLOCK = 35
VIX_REDUCE_HALF = 25
VIX_REDUCE_QUARTER = 30
VIX_SPREADS_ONLY = 30

# Level scores (V3.1 — re-weighted for institutional reality)
SCORE_52W = 10
SCORE_MONTHLY = 9
SCORE_PD_POC = 9        # Previous day POC — highest institutional weight
SCORE_WEEKLY = 8
SCORE_PDH_PDL = 8
SCORE_PD_VAH_VAL = 8   # Previous day VAH/VAL — settled institutional boundaries
SCORE_PDC = 7
SCORE_ORH_ORL = 7
SCORE_POC = 7           # Today's developing POC (was 6)
SCORE_VAH_VAL = 6       # Today's developing VAH/VAL — passes 7 only with confluence
SCORE_HVN = 5
SCORE_PMH_PML = 5       # Fails 7 unless HVN-aligned (5*1.5=7.5) or confluence
SCORE_VWAP = 4
SCORE_ROUND_10 = 4
SCORE_ROUND_5 = 3
SCORE_ZONE_STRONG = 9
SCORE_ZONE_MEDIUM = 8
SCORE_ZONE_WEAK = 7
CONFLUENCE_BOOST = 2

# Data
DAILY_BARS_HISTORY = 252
ZONE_LOOKBACK_DAYS = 60
ZONE_SWING_BARS = 5
ZONE_MIN_TESTS = 2
ZONE_CLUSTER_PCT = 0.01
YFINANCE_STAGGER_SEC = 0.5

# Data health & validation
VALIDATE_INTERVAL_SEC = 300       # yfinance validation every 5 min
BACKFILL_MAX_GAP_SEC = 600        # max gap to backfill (10 min)
HEALTH_STALE_SEC = 30             # STALE after 30s no ticks
HEALTH_DEGRADED_BARS = 3          # DEGRADED if 3+ bars backfilled
VOL_DIVERGENCE_THRESHOLD = 0.20   # 20% volume divergence = warning
PRICE_DIVERGENCE_THRESHOLD = 0.001  # 0.1% price divergence = warning
YF_MAX_CONCURRENT = 2             # max concurrent yfinance HTTP calls
YF_DELAY_MINUTES = 15             # yfinance data delay
YF_VALIDATE_WINDOW = 5            # validate 5-min window (T-20 to T-15)
HEARTBEAT_SEC = 1                 # bar close heartbeat interval

# GPT
GPT_MODEL = "gpt-5.4"
GPT_MAX_TOOL_CALLS = 10
