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
                engine.signals.force_reset_cooldown()
                if engine.state.factor_engine:
                    asyncio.create_task(engine._on_candle_close())

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
    engine.signals.force_reset_cooldown()
    asyncio.create_task(engine._on_candle_close())
    return {"status": "triggered"}
