import json
import pytz
import aiohttp
from datetime import datetime
from .tools import TOOLS, ToolHandler

ET = pytz.timezone("America/New_York")

SYSTEM_PROMPT = """You are a professional SPY day trader with 10 years of experience.

Price is near a key level. Use your tools to investigate the situation.
Think step by step like a real trader.

YOUR PROCESS:
1. Check the candles — what is price doing at the level?
2. Check CVD — is the move real or fake?
3. Check the level — how strong is it?
4. Check trend — which direction should you trade?
5. Check all levels — where are your targets?
6. Calculate RR — is it worth trading?
7. Send signal — LONG, SHORT, or WAIT

RULES:
- Only trade in direction of 15m trend
- CVD must confirm the move
- RR must be at least 2.0 before sending signal
- If setup is unclear → send WAIT with explanation
- Never force a trade — WAIT is a valid answer
- Stop loss must be at a logical level (below/above key level)
- Target must be the next key level

ADDITIONAL TOOLS (only if needed):
  get_level_info   — level test history
  get_all_levels   — find targets
  get_vwap_story   — full day VWAP context
  get_or_status    — opening range status
  calculate_rr     — confirm RR before entry

You are making real trading decisions. Be thorough but decisive."""


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
    MAX_TOOL_CALLS = 15

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
