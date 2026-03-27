import asyncio
import json
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from .engine import get_engine

router = APIRouter()
HTML = os.path.join(os.path.dirname(__file__), "..", "frontend", "spy-trader.html")


@router.get("/spy-trader")
async def serve_page():
    return FileResponse(HTML)


@router.websocket("/ws/spy")
async def browser_websocket(ws: WebSocket):
    await ws.accept()
    engine = get_engine()
    await engine.add_client(ws)

    try:
        while True:
            msg = await ws.receive_json()

            if msg.get("type") == "reanalyze":
                if not engine._generating:
                    asyncio.create_task(engine._on_candle_close())

            elif msg.get("type") == "settings":
                if "minRR" in msg:
                    import spy.market_utils as mu
                    mu.MIN_RR = float(msg["minRR"])
                if "cooldown" in msg:
                    engine.cooldown_secs = int(msg["cooldown"]) * 60
                if "orDuration" in msg:
                    import spy.sessions as sess_mod
                    sess_mod.OR_DURATION_MINS = int(msg["orDuration"])
                await ws.send_json({"type": "settings_ack"})

            elif msg.get("type") == "ping":
                await ws.send_json({"type": "pong"})

    except WebSocketDisconnect:
        await engine.remove_client(ws)
    except Exception:
        await engine.remove_client(ws)


@router.get("/api/spy/state")
async def get_state():
    engine = get_engine()
    return engine._get_push_payload()


@router.post("/api/spy/signal")
async def force_signal():
    engine = get_engine()
    if not engine._generating:
        asyncio.create_task(engine._on_candle_close())
    return {"status": "triggered"}


@router.get("/api/spy/simulate")
async def simulate_agent():
    """Run agent through real broadcast path so browser sees everything live."""
    engine = get_engine()
    if engine._generating:
        return {"status": "BUSY", "reason": "Agent already running"}
    if not engine._openai_key:
        return {"status": "SKIP", "reason": "No OpenAI key"}
    from .models import Level
    price = engine.candles.live_price
    if not price and engine.candles.c1m:
        price = engine.candles.c1m[-1].c
    if not price:
        return {"status": "SKIP", "reason": "No price data"}
    # Use nearest real level, or create a mock PDH
    level = None
    for lv in engine._current_levels:
        if lv.label in ("PDH", "PDL", "ORH", "ORL", "VWAP"):
            level = lv
            break
    if not level:
        level = Level(price=price + 0.50, label="PDH", type="resistance", strength=4, source="PD")
    engine._fire_agent(level, "REJECTION")
    return {"status": "STARTED", "level": level.label, "price": level.price}


@router.get("/api/spy/debug-tool")
async def debug_tool(tool: str = "get_all_levels", args: str = "{}"):
    import json as j
    from .agent.tools import ToolHandler
    engine = get_engine()
    handler = ToolHandler(engine)
    parsed = j.loads(args)
    result = await handler.execute(tool, parsed)
    return {"tool": tool, "result": result}


@router.get("/api/spy/test-tools")
async def test_tools():
    from .agent.tools import ToolHandler
    engine = get_engine()
    handler = ToolHandler(engine)
    results = await handler.run_all_tool_tests()
    passed = sum(1 for v in results.values() if v == "PASS")
    failed = sum(1 for v in results.values() if v != "PASS")
    return {"summary": f"{passed} passed, {failed} failed", "results": results}


@router.get("/api/spy/test-levels")
async def test_levels():
    engine = get_engine()
    levels = engine._current_levels
    price = engine.candles.live_price
    issues = []
    if len(levels) < 4:
        issues.append(f"Too few levels: {len(levels)}")
    labels = [lvl.label for lvl in levels]
    for required in ["PDH", "PDL", "VWAP"]:
        if not any(required in n for n in labels):
            issues.append(f"Missing {required}")
    zero = [lvl.label for lvl in levels if lvl.price <= 0]
    if zero:
        issues.append(f"Zero price levels: {zero}")
    if price > 0:
        bad = [lvl.label for lvl in levels if abs(lvl.price - price) / price > 0.20]
        if bad:
            issues.append(f"Levels far from price: {bad}")
    pdh = next((lvl for lvl in levels if lvl.label == "PDH"), None)
    pdl = next((lvl for lvl in levels if lvl.label == "PDL"), None)
    if pdh and pdl and pdh.price <= pdl.price:
        issues.append(f"PDH {pdh.price} <= PDL {pdl.price}")
    return {
        "total_levels": len(levels),
        "current_price": price,
        "issues": issues,
        "status": "PASS" if not issues else "FAIL",
        "levels": [{"label": lvl.label, "price": lvl.price, "strength": lvl.strength, "type": lvl.type}
                   for lvl in sorted(levels, key=lambda x: x.price, reverse=True)],
    }


@router.get("/api/spy/test-cvd")
async def test_cvd():
    engine = get_engine()
    cvd = engine.cvd
    original_value = cvd._cvd
    original_last = cvd._last_price
    original_date = cvd._session_date
    from .sessions import get_et_now
    cvd._session_date = get_et_now().strftime("%Y-%m-%d")
    cvd._last_price = 580.00
    cvd._cvd = 0.0
    cvd.process_trade(580.10, 100)
    after_up = cvd._cvd
    cvd.process_trade(579.90, 100)
    after_down = cvd._cvd
    before_same = cvd._cvd
    cvd.process_trade(579.90, 100)
    after_same = cvd._cvd
    results = {
        "tick_rule_up": "PASS" if after_up > 0 else "FAIL: CVD didn't increase on up tick",
        "tick_rule_down": "PASS" if after_down < after_up else "FAIL: CVD didn't decrease on down tick",
        "tick_rule_same": "PASS" if after_same == before_same else "FAIL: CVD changed on equal price",
        "current_value": original_value,
        "bias": cvd.bias,
        "history_points": len(cvd._history),
    }
    cvd._cvd = original_value
    cvd._last_price = original_last
    cvd._session_date = original_date
    passed = sum(1 for k, v in results.items() if v == "PASS")
    results["summary"] = f"{passed}/3 passed"
    return results


@router.get("/api/spy/test-gates")
async def test_gates():
    import time
    engine = get_engine()
    from .models import Level
    level = Level(price=580.00, label="GATE_TEST", type="resistance", strength=3, source="TEST")
    results = {}
    passed, _ = engine._gates_pass(level, 1000, 1000)
    results["outside_hours_blocked"] = "PASS" if not passed else "FAIL: should block outside hours"
    orig_time = engine._last_signal_time
    engine._last_signal_time = time.time()
    passed, _ = engine._gates_pass(level, 1000, 1000)
    results["cooldown_blocked"] = "PASS" if not passed else "FAIL: cooldown not blocking"
    engine._last_signal_time = orig_time
    orig_lvl = engine._last_level_times.get("GATE_TEST", 0)
    engine._last_level_times["GATE_TEST"] = time.time()
    passed, _ = engine._gates_pass(level, 1000, 1000)
    results["level_cooldown_blocked"] = "PASS" if not passed else "FAIL: level cooldown not blocking"
    engine._last_level_times["GATE_TEST"] = orig_lvl
    passed, _ = engine._gates_pass(level, 100, 1000)
    results["low_volume_blocked"] = "PASS" if not passed else "FAIL: low volume not blocking"
    pc = sum(1 for v in results.values() if v == "PASS")
    results["summary"] = f"{pc}/{len(results)} gates working"
    return results


@router.get("/api/spy/test-agent")
async def test_agent():
    engine = get_engine()
    if not engine._openai_key:
        return {"status": "SKIP", "reason": "No OpenAI key"}
    price = engine.candles.live_price or 580.0
    tool_calls = []
    tokens_received = []
    signal_result = {}
    error_result = [None]
    from .agent.agent import run_agent
    from .agent.tools import ToolHandler
    original_execute = ToolHandler.execute

    async def tracked_execute(self, name, args):
        tool_calls.append(name)
        if name == "send_signal":
            signal_result.update(args)
            return await original_execute(self, name, args)
        return await original_execute(self, name, args)

    ToolHandler.execute = tracked_execute
    try:
        initial_message = (
            f"TEST MODE — SPY ${price:.2f}\n"
            f"Near PDH ${price + 0.50:.2f}\n"
            f"REJECTION detected.\n"
            f"Investigate and decide. Call send_signal with your conclusion."
        )

        async def _on_token(t):
            tokens_received.append(t)

        async def _on_complete(a):
            if a:
                signal_result.update(a)

        async def _on_error(e):
            error_result[0] = e

        async def _on_tool(name, status, args, result):
            pass

        await run_agent(
            engine=engine, initial_message=initial_message,
            openai_key=engine._openai_key, model=engine.model, reasoning="low",
            on_token=_on_token,
            on_tool_call=_on_tool,
            on_complete=_on_complete,
            on_error=_on_error,
        )
    except Exception as e:
        error_result[0] = str(e)
    finally:
        ToolHandler.execute = original_execute
    return {
        "status": "PASS" if not error_result[0] else "FAIL",
        "error": error_result[0],
        "tool_calls": tool_calls,
        "tool_count": len(tool_calls),
        "tokens_received": len(tokens_received),
        "signal": signal_result.get("signal", "none"),
        "model_used": engine.model,
    }


@router.get("/api/spy/test-telegram")
async def test_telegram():
    engine = get_engine()
    if not engine._telegram_token:
        return {"status": "SKIP", "reason": "No Telegram token in .env"}
    await engine._send_telegram({
        "signal": "LONG", "confidence": "HIGH", "entry": 580.50, "stop": 579.00,
        "tp1": 582.00, "tp2": 584.00, "rr": 2.8,
        "narrative": "System test — ignore this signal", "invalidation": "Close below $579.00",
    })
    return {"status": "SENT", "message": "Check your Telegram app"}


@router.get("/api/spy/test-data")
async def test_data():
    engine = get_engine()
    candles = engine.candles
    results = {}
    results["1m_bars"] = "PASS" if len(candles.c1m) > 100 else f"FAIL: only {len(candles.c1m)} bars"
    results["5m_bars"] = "PASS" if len(candles.c5m) > 50 else f"FAIL: only {len(candles.c5m)} bars"
    results["15m_bars"] = "PASS" if len(candles.c15m) > 20 else f"FAIL: only {len(candles.c15m)} bars"
    results["daily_bars"] = "PASS" if len(candles.c_daily) > 50 else f"FAIL: only {len(candles.c_daily)} bars"
    results["live_price"] = "PASS" if candles.live_price > 0 else "FAIL: no live price"
    results["closed_1m_excludes_live"] = "PASS" if len(candles.closed_1m) == len(candles.c1m) - 1 else "FAIL"
    zero_1m = sum(1 for c in candles.c1m if c.c <= 0)
    results["no_zero_prices"] = "PASS" if zero_1m == 0 else f"FAIL: {zero_1m} zero price candles"
    vix = engine.vix_val
    results["vix_reasonable"] = "PASS" if vix and 5 < vix < 100 else f"FAIL: VIX={vix}"
    ts_1m = [c.t for c in candles.c1m]
    results["1m_chronological"] = "PASS" if ts_1m == sorted(ts_1m) else "FAIL: not in order"
    passed = sum(1 for v in results.values() if v == "PASS")
    results["summary"] = f"{passed}/{len(results) - 1} passed"
    return results


@router.get("/api/spy/test-patterns")
async def test_patterns():
    from .models import Candle, Level
    engine = get_engine()
    results = {}
    lvl_r = Level(price=580.00, label="TEST_R", type="resistance", strength=3, source="TEST")
    lvl_s = Level(price=580.00, label="TEST_S", type="support", strength=3, source="TEST")

    # Bearish rejection at resistance
    c1 = Candle(t=0, o=579.50, h=580.10, l=579.40, c=579.60, v=1000)
    results["bearish_rejection"] = "PASS" if engine._is_rejection_candle(c1, lvl_r) else "FAIL"

    # Bullish rejection at support
    c2 = Candle(t=0, o=580.50, h=580.60, l=579.90, c=580.40, v=1000)
    results["bullish_rejection"] = "PASS" if engine._is_rejection_candle(c2, lvl_s) else "FAIL"

    # Not rejection (wick too small relative to body)
    c3 = Candle(t=0, o=579.60, h=580.05, l=579.50, c=579.90, v=800)
    results["not_rejection"] = "PASS" if not engine._is_rejection_candle(c3, lvl_r) else "FAIL"

    # Doji — should NOT trigger
    doji = Candle(t=0, o=580.10, h=580.80, l=579.20, c=580.12, v=500)
    results["doji_no_stop_hunt"] = "PASS" if not engine._is_stop_hunt_candle(doji, lvl_r) else "FAIL"

    # Real breakdown not stop hunt
    bd = Candle(t=0, o=580.50, h=580.80, l=579.50, c=579.70, v=1000)
    results["breakdown_not_stop_hunt"] = "PASS" if not engine._is_stop_hunt_candle(bd, lvl_s) else "FAIL"

    # Small wick not rejection (wick < 2x body)
    sw = Candle(t=0, o=579.60, h=580.02, l=579.50, c=579.80, v=800)
    results["small_wick_no_rejection"] = "PASS" if not engine._is_rejection_candle(sw, lvl_r) else "FAIL"

    # Far candle not rejection
    far = Candle(t=0, o=577.00, h=578.00, l=576.50, c=577.50, v=800)
    results["far_no_rejection"] = "PASS" if not engine._is_rejection_candle(far, lvl_r) else "FAIL"

    # Retest: unbroken level should not trigger
    engine._broken_levels.pop("TEST_R", None)
    rt = Candle(t=0, o=580.30, h=581.00, l=579.98, c=580.05, v=1000)
    results["unbroken_no_retest"] = "PASS" if not engine._is_retest_candle(rt, lvl_r) else "FAIL"

    # Bearish retest: confirmed broken level, held below
    engine._broken_levels["TEST_R"] = {"time": 0, "closes_since": 3, "confirmed": True}
    brt = Candle(t=0, o=579.50, h=580.02, l=579.20, c=579.60, v=1000)
    results["bearish_retest"] = "PASS" if engine._is_retest_candle(brt, lvl_r) else "FAIL"

    engine._broken_levels.pop("TEST_R", None)
    passed = sum(1 for v in results.values() if v == "PASS")
    results["summary"] = f"{passed}/{len(results) - 1} passed"
    return results
