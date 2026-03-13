import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

from agent.graph import agent

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
    yield
    task.cancel()


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
