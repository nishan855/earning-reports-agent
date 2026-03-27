import json
import pytz
import aiohttp
from datetime import datetime
from .tools import TOOLS, ToolHandler

ET = pytz.timezone("America/New_York")

SYSTEM_PROMPT = """You are a professional SPY day trader with 10 years of experience.

A pattern was detected at a key level. You have a MARKET SNAPSHOT with candles, CVD, trend, levels, and session already provided. Review it first.

YOUR PROCESS:
1. REVIEW the snapshot — you already have candles, CVD, trend, levels, session
2. If needed, call get_level_info for confluence/test history on the specific level
3. Identify entry, stop, and target from the levels map
4. Call calculate_rr to confirm RR >= 2.0
5. Call send_signal with your decision

DO NOT call get_candles, get_cvd, get_trend, get_all_levels, or get_session — that data is already in the snapshot. Only call tools for ADDITIONAL info you don't have.

RULES:
- Only trade in direction of 15m trend
- CVD must confirm the move (no divergence against you)
- RR must be at least 2.0
- If setup is unclear → send WAIT with what to watch for
- Never force a trade — WAIT is always valid
- Stop loss below/above a meaningful level
- Target at the next key level

AVAILABLE TOOLS (use sparingly):
  get_level_info   — deep dive on a specific level
  get_vwap_story   — full day VWAP behavior
  get_or_status    — opening range context
  calculate_rr     — REQUIRED before send_signal
  send_signal      — your final decision

Be decisive. You have the data. Make the call."""


async def run_agent(
    engine,
    initial_message: str,
    openai_key: str,
    model: str,
    reasoning: str,
    on_token,
    on_tool_call,   # async (name, status, args, result)
    on_complete,
    on_error,
) -> None:
    tool_handler = ToolHandler(engine)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": initial_message},
    ]

    tool_call_count = 0
    MAX_TOOL_CALLS = 8

    headers = {
        "Authorization": f"Bearer {openai_key}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            while tool_call_count < MAX_TOOL_CALLS:
                payload = {
                    "model": model,
                    "messages": messages,
                    "tools": TOOLS,
                    "tool_choice": "auto",
                    "stream": True,
                }

                async with session.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers=headers,
                    json=payload,
                ) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        await on_error(f"OpenAI error {resp.status}: {err[:200]}")
                        return

                    full_content = ""
                    tool_calls_raw = {}
                    finish_reason = None

                    async for line in resp.content:
                        line = line.decode().strip()
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data == "[DONE]":
                            break

                        try:
                            chunk = json.loads(data)
                            delta = chunk["choices"][0].get("delta", {})
                            finish_reason = chunk["choices"][0].get("finish_reason") or finish_reason

                            if "content" in delta and delta["content"]:
                                full_content += delta["content"]
                                await on_token(delta["content"])

                            if "tool_calls" in delta:
                                for tc in delta["tool_calls"]:
                                    idx = tc["index"]
                                    if idx not in tool_calls_raw:
                                        tool_calls_raw[idx] = {"id": "", "name": "", "args_str": ""}
                                    if tc.get("id"):
                                        tool_calls_raw[idx]["id"] = tc["id"]
                                    if tc.get("function", {}).get("name"):
                                        tool_calls_raw[idx]["name"] = tc["function"]["name"]
                                    tool_calls_raw[idx]["args_str"] += tc.get("function", {}).get("arguments", "")
                        except Exception:
                            pass

                if full_content or tool_calls_raw:
                    assistant_msg = {"role": "assistant"}
                    if full_content:
                        assistant_msg["content"] = full_content
                    if tool_calls_raw:
                        assistant_msg["tool_calls"] = [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {"name": tc["name"], "arguments": tc["args_str"]},
                            }
                            for tc in tool_calls_raw.values()
                        ]
                    messages.append(assistant_msg)

                if not tool_calls_raw:
                    await on_complete(None)
                    return

                for tc in tool_calls_raw.values():
                    tool_call_count += 1
                    try:
                        args = json.loads(tc["args_str"] or "{}")
                    except Exception:
                        args = {}

                    name = tc["name"]
                    print(f"[SPY] [TOOL] {name}({json.dumps(args)[:80]})")
                    await on_tool_call(name, "running", args, "")
                    result = await tool_handler.execute(name, args)
                    await on_tool_call(name, "complete", args, str(result)[:500])

                    if name == "send_signal":
                        await on_complete(args)
                        return

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": str(result),
                    })

            await on_error(f"Max tool calls ({MAX_TOOL_CALLS}) reached — defaulting to WAIT")

    except Exception as e:
        await on_error(f"Agent error: {str(e)}")
