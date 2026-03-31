import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager

import os
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

from agent.graph import agent
from spy.router import router as spy_router
# from spy.engine import get_engine  # OLD SPY ENGINE — DISABLED

from trading.core.multi_engine import MultiEngine
from trading.constants import ASSETS
from trading.models import Signal

CACHE_TTL = 86400
_cache: dict[str, dict] = {}

RATE_LIMIT = 3
_ip_usage: dict[str, list[float]] = {}

def _get_ip_remaining(ip: str) -> int:
    now = time.time()
    if ip in _ip_usage:
        _ip_usage[ip] = [ts for ts in _ip_usage[ip] if now - ts < CACHE_TTL]
    return RATE_LIMIT - len(_ip_usage.get(ip, []))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("qern")


async def _cache_cleanup_loop():
    while True:
        await asyncio.sleep(3600)
        now = time.time()
        stale = [k for k, v in _cache.items() if now - v["ts"] >= CACHE_TTL]
        for k in stale:
            del _cache[k]
        if stale:
            logger.info(f"Cache cleanup: evicted {len(stale)} stale entries")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("Qern API ready")
    task = asyncio.create_task(_cache_cleanup_loop())

    # ── OLD SPY ENGINE — DISABLED ──────────────
    # Replaced by trading/ multi-asset system.
    # await get_engine().start()
    # ────────────────────────────────────────────

    # ── NEW TRADING ENGINE ─────────────────────
    sim_mode = os.getenv("SIM_MODE", "").lower() in ("1", "true", "yes")
    logger.info("=" * 50)
    logger.info("OLD SPY ENGINE:    DISABLED")
    logger.info(f"NEW TRADING ENGINE: {'SIMULATION' if sim_mode else 'LIVE'}")
    logger.info("Dashboard:  /trading")
    logger.info("WebSocket:  /ws/trading")
    if sim_mode:
        logger.info("Simulate:   GET /trading/simulate?speed=0.05")
    logger.info("=" * 50)
    if sim_mode:
        trading_task = asyncio.create_task(trading_engine.start_simulation())
    else:
        trading_task = asyncio.create_task(trading_engine.start())

    yield
    task.cancel()
    trading_task.cancel()


app = FastAPI(
    title="Qern API",
    version="1.0.0",
    description="AI-Powered Earnings Intelligence Agent",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/frontend", StaticFiles(directory="frontend"), name="frontend")
app.include_router(spy_router)


@app.get("/")
async def root():
    return FileResponse("frontend/index.html")


@app.get("/health")
async def health():
    return {"status": "UP"}


@app.get("/rate-limit")
async def rate_limit(request: Request):
    ip = request.client.host if request.client else "unknown"
    remaining = _get_ip_remaining(ip)
    return {"remaining": remaining, "limit": RATE_LIMIT}


def _build_response(result: dict, cached: bool = False) -> dict:
    return {
        "cached": cached,
        "ticker": result.get("ticker"),
        "company": result.get("company_name") or result.get("company"),
        "current_quarter": result.get("current_quarter"),
        "current_date": result.get("current_date"),
        "signal": result.get("signal"),
        "confidence": result.get("confidence"),
        "reasoning": result.get("reasoning"),
        "price_target": result.get("price_target"),
        "price_target_timeframe": result.get("price_target_timeframe"),
        "upside_downside": result.get("upside_downside"),
        "current_price": result.get("current_price"),
        "fifty_two_week_high": result.get("fifty_two_week_high"),
        "fifty_two_week_low": result.get("fifty_two_week_low"),
        "market_cap": result.get("market_cap"),
        "sector": result.get("sector"),
        "pe_ratio": result.get("pe_ratio"),
        "forward_pe": result.get("forward_pe"),
        "trailing_eps": result.get("trailing_eps"),
        "forward_eps": result.get("forward_eps"),
        "revenue_growth": result.get("revenue_growth"),
        "gross_margin": result.get("gross_margin"),
        "analyst_target_price": result.get("analyst_target_price"),
        "analyst_consensus": result.get("analyst_consensus"),
        "num_analysts": result.get("num_analysts"),
        "short_interest": result.get("short_interest"),
        "short_percent_float": result.get("short_percent_float"),
        "insider_ownership": result.get("insider_ownership"),
        "credibility_score": result.get("credibility_score"),
        "sentiment_trajectory": result.get("sentiment_trajectory"),
        "risks": result.get("risks"),
        "catalysts": result.get("catalysts"),
        "anomalies": result.get("anomalies"),
        "language_shifts": result.get("language_shifts"),
        "guidance_history": result.get("guidance_history"),
        "next_earnings_date": result.get("next_earnings_date"),
        "quarterly_revenue": result.get("quarterly_revenue"),
        "quarterly_eps": result.get("quarterly_eps"),
        "competitor_data": result.get("competitor_data"),
        "report": result.get("report"),
    }


@app.get("/analyze/{ticker}")
async def analyze(ticker: str, request: Request):
    ticker = ticker.upper().strip()
    if not re.match(r"^[A-Z]{1,6}$", ticker):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid ticker '{ticker}'. Must be 1-6 letters only.",
        )

    ip = request.client.host if request.client else "unknown"

    now = time.time()
    if ticker in _cache:
        entry = _cache[ticker]
        if now - entry["ts"] < CACHE_TTL:
            logger.info(f"Cache HIT for {ticker} (age: {int(now - entry['ts'])}s)")
            return entry["data"]
        else:
            del _cache[ticker]

    remaining = _get_ip_remaining(ip)
    if remaining <= 0:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. You can generate up to 3 reports per 24 hours.",
        )

    _ip_usage.setdefault(ip, []).append(now)
    logger.info(f"Rate limit: {ip} has {remaining - 1} reports remaining")

    try:
        result = await agent.ainvoke({"ticker": ticker})
    except Exception as e:
        logger.error(f"Agent error for {ticker}: {e}")
        raise HTTPException(status_code=500, detail=f"Analysis failed: {str(e)}")

    if result.get("company_name") == "Unknown":
        raise HTTPException(
            status_code=404,
            detail=f"Company not found for ticker '{ticker}'.",
        )

    response = _build_response(result, cached=False)

    _cache[ticker] = {"data": {**response, "cached": True}, "ts": now}
    logger.info(f"Cache STORE for {ticker}")

    return response


# ══════════════════════════════════════════════════
# NEW MULTI-ASSET TRADING ENGINE
# ══════════════════════════════════════════════════

class TradingWSManager:
    def __init__(self):
        self._clients: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._clients.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self._clients:
            self._clients.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for client in self._clients:
            try:
                await client.send_json(data)
            except Exception:
                dead.append(client)
        for d in dead:
            self.disconnect(d)


trading_ws = TradingWSManager()

from trading.notifications.telegram import TelegramNotifier
telegram = TelegramNotifier()


async def _on_tick(data: dict):
    await trading_ws.broadcast(data)


_all_signals: list[dict] = []


def _signal_to_dict(signal: Signal) -> dict:
    return {
        "asset": signal.asset, "direction": signal.direction,
        "confidence": signal.confidence, "confidence_pct": signal.confidence_pct,
        "approach_type": signal.approach_type,
        "pattern": signal.pattern,
        "level_name": signal.level_name, "level_price": signal.level_price,
        "entry": signal.entry, "stop": signal.stop,
        "tp1": signal.tp1, "tp2": signal.tp2, "rr": signal.rr,
        "option_type": signal.option_type, "strike": signal.strike,
        "expiry_date": signal.expiry_date, "dte": signal.dte, "size": signal.size,
        "est_premium_lo": signal.est_premium_lo, "est_premium_hi": signal.est_premium_hi,
        "breakeven": signal.breakeven, "instrument": signal.instrument,
        "narrative": signal.narrative, "reasoning": signal.reasoning,
        "invalidation": signal.invalidation, "warnings": signal.warnings,
        "wait_for": signal.wait_for, "fired_at": signal.fired_at,
        "session": signal.session, "vix_at_signal": signal.vix_at_signal,
        "timestamp": time.time(),
    }


async def _on_signal(signal: Signal):
    sig_dict = _signal_to_dict(signal)

    # Store in memory (all signals including WAIT)
    _all_signals.append(sig_dict)
    if len(_all_signals) > 200:
        _all_signals[:] = _all_signals[-200:]

    # Send to Telegram (skip WAIT signals)
    if signal.direction != "WAIT":
        try:
            await telegram.send_signal(signal)
        except Exception as e:
            logger.error(f"[Telegram] Error: {e}")

    # Broadcast to WebSocket dashboard (all signals)
    await trading_ws.broadcast({
        "type": "signal_complete",
        "asset": signal.asset,
        "signal": sig_dict,
    })


async def _on_state(data: dict):
    await trading_ws.broadcast(data)


trading_engine = MultiEngine(
    finnhub_key=os.getenv("FINNHUB_KEY", ""),
    openai_key=os.getenv("OPENAI_KEY", ""),
    on_signal=_on_signal,
    on_tick=_on_tick,
    on_state=_on_state,
)


@app.get("/trading")
async def trading_dashboard():
    return FileResponse("frontend/trading.html")


@app.websocket("/ws/trading")
async def trading_websocket(ws: WebSocket):
    await trading_ws.connect(ws)
    try:
        assets_state = [trading_engine.get_asset_state(a) for a in ASSETS]
        await ws.send_json({"type": "state", "vix": trading_engine._vix, "session": trading_engine._get_session_label(), "assets": assets_state})
        # Send bars + levels for all 8 assets on connect
        for asset in ASSETS:
            try:
                detail = trading_engine.get_asset_state(asset, include_bars=True)
                await ws.send_json({"type": "asset_detail", "asset": asset, "data": detail})
            except Exception:
                pass
    except Exception as e:
        logger.error(f"[WS] Initial state error: {e}")
    try:
        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_json({"type": "pong"})
            elif msg.startswith("select:"):
                asset = msg.split(":")[1].strip().upper()
                if asset in ASSETS:
                    detail = trading_engine.get_asset_state(asset, include_bars=True)
                    await ws.send_json({"type": "asset_detail", "asset": asset, "data": detail})
    except WebSocketDisconnect:
        trading_ws.disconnect(ws)
    except Exception:
        trading_ws.disconnect(ws)


@app.get("/trading/state")
async def trading_state():
    try:
        assets = [trading_engine.get_asset_state(a) for a in ASSETS]
        return {"status": "ok", "vix": trading_engine._vix, "session": trading_engine._get_session_label(), "assets": assets}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.get("/trading/health")
async def trading_health():
    """Per-asset data health: HEALTHY, DEGRADED, or STALE."""
    try:
        return {
            "status": "ok",
            "assets": {
                a: {
                    "data_health": trading_engine._health[a].status,
                    "bars_backfilled": trading_engine._health[a].bars_backfilled,
                    "ws_disconnects": trading_engine._health[a].ws_disconnects,
                    "last_validated": trading_engine._health[a].last_validated_at,
                }
                for a in ASSETS
            },
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.get("/trading/signals")
async def trading_signals(asset: str = ""):
    """All agent decisions (LONG, SHORT, WAIT) for today. Filter with ?asset=SPY."""
    if asset:
        return [s for s in _all_signals if s["asset"].upper() == asset.upper()]
    return _all_signals


@app.get("/trading/simulate")
async def trading_simulate(speed: float = 0.05, day: int = 0):
    """Replay historical data through the full pipeline (legacy — bypasses tick pipeline).
    speed = seconds between each 1m candle (0.1 = ~40s for full day).
    day = 0 for most recent, 1 for day before, 2 for 2 days ago, etc."""
    asyncio.create_task(trading_engine.run_simulation(speed=speed, day_offset=day))
    return {"status": "started", "speed": speed, "day_offset": day, "note": "Watch /trading dashboard"}


@app.get("/trading/simulate/ticks")
async def trading_simulate_ticks(speed: float = 0.01, day: int = 0, minutes: int = 60):
    """Simulate 1 hour of Finnhub WebSocket ticks through the REAL tick pipeline.
    Tests: tick→1m bars→5m/15m aggregation→heartbeat→detection→signals.
    speed = seconds between each tick (0.01 = ~6 min for 1 hour).
    day = 0 for most recent trading day.
    minutes = how many minutes to replay (default 60)."""
    asyncio.create_task(trading_engine.run_tick_simulation(speed=speed, day_offset=day, minutes=minutes))
    est_ticks = minutes * 10 * 8  # ~10 ticks per bar × 8 assets
    est_seconds = int(est_ticks * speed)
    return {
        "status": "started",
        "speed": speed,
        "day_offset": day,
        "minutes": minutes,
        "est_ticks": est_ticks,
        "est_runtime_sec": est_seconds,
        "note": "Watch /trading dashboard — bars build in real-time from ticks",
    }
