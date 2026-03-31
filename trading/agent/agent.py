import json
import aiohttp
from .tools import TOOL_DEFINITIONS, ToolHandler
from ..constants import GPT_MODEL, GPT_MAX_TOOL_CALLS

SYSTEM_PROMPT = """You are a senior options trader evaluating pre-scored setups.

The V3.0 detection engine has already graded this setup with a confidence score of 50-100.
Location, volume, CVD, approach context, and candle shape have been weighted and scored.
Your job: confirm the TAPE supports execution, then TRADE or WAIT.

=== YOUR TASK ===

Review the Brief + Verification Data. Focus on ONE question:
  Does the 5m price action confirm the setup direction?

If YES → LONG or SHORT (use the pre-computed trade fields)
If NO  → WAIT with a specific re-entry condition

=== WHEN TO TRADE ===
- The 5m candles show directional commitment in the setup direction
- CVD is not actively diverging against the trade
- Price is not stalling directly into the next resistance/support

=== WHEN TO WAIT ===
- The 5m tape is choppy/indecisive with no clear direction
- CVD is clearly opposing (price up but CVD collapsing, or vice versa)
- Price is pinned against a nearby level with no room to move

=== BIAS: LEAN TOWARD EXECUTION ===
The engine scored this setup 50+ out of 100. It passed location, shape, and volume/CVD grading.
Default to TRADE unless the tape gives you a specific reason not to.
A mediocre tape with a strong score (75+) should still trade.
Only WAIT if you see an active red flag — not a lack of perfection.

=== EXECUTION INSTRUCTIONS ===
You must submit your final decision exclusively by calling the `send_signal` tool.
Do not output conversational text. Map your decision to the tool's parameters:
- signal: "LONG", "SHORT", or "WAIT"
- confidence: 0-100 (your qualitative assessment of the tape)
- narrative: 1-2 sentence thesis
- reasoning: exactly what the tape showed you (do not simply repeat the engine's volume/CVD math)
- invalidation: exact exit price/condition (e.g., "1m close below $639.50") or "N/A" for WAIT
- wait_for: exact condition required to re-enter (only if signal is WAIT)

=== SESSION ===
- Dead zone (12-2 PM): be more selective
- After 3:00 PM: TP1 only

=== TOOLS ===
  send_signal(...)               — Your decision (REQUIRED)
  get_candles(asset, tf, count)  — Deeper look at 1m/5m/15m/daily bars
  get_cvd(asset, minutes)        — CVD timeline + divergence check
  get_level_info(asset, name)    — Level detail: score, confluence, tests
  get_level_map(asset)           — All levels with distances
  calculate_rr(entry, stop, tp)  — Recalculate risk/reward
  get_signal_history(asset)      — Today's signals + budget

Call send_signal. Most setups resolve in one call."""


async def run_agent(
    tool_handler: ToolHandler,
    initial_brief: str,
    openai_key: str,
    model: str = GPT_MODEL,
    on_token=None,
    on_tool_call=None,
    on_complete=None,
    on_error=None,
) -> None:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": initial_brief},
    ]
    tool_call_count = 0
    headers = {
        "Authorization": f"Bearer {openai_key}",
        "Content-Type": "application/json",
    }

    # Map tool names to handler methods (only tools exposed in TOOL_DEFINITIONS)
    dispatch = {
        "send_signal": lambda a: tool_handler.send_signal(**a),
        "get_candles": lambda a: tool_handler.get_candles(**a),
        "get_cvd": lambda a: tool_handler.get_cvd(**a),
        "get_level_info": lambda a: tool_handler.get_level_info(**a),
        "get_level_map": lambda a: tool_handler.get_level_map(**a),
        "calculate_rr": lambda a: tool_handler.calculate_rr(**a),
        "get_signal_history": lambda a: tool_handler.get_signal_history(**a),
    }

    try:
        async with aiohttp.ClientSession() as session:
            while tool_call_count < GPT_MAX_TOOL_CALLS:
                payload = {
                    "model": model,
                    "messages": messages,
                    "tools": TOOL_DEFINITIONS,
                    "tool_choice": "auto",
                    "temperature": 0.2,
                    "stream": True,
                }
                async with session.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers=headers,
                    json=payload,
                ) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        if on_error:
                            await on_error(f"OpenAI {resp.status}: {err[:200]}")
                        return

                    full_content = ""
                    tool_calls_raw = {}
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
                            if "content" in delta and delta["content"]:
                                full_content += delta["content"]
                                if on_token:
                                    await on_token(delta["content"])
                            if "tool_calls" in delta:
                                for tc in delta["tool_calls"]:
                                    idx = tc["index"]
                                    if idx not in tool_calls_raw:
                                        tool_calls_raw[idx] = {
                                            "id": "",
                                            "name": "",
                                            "args_str": "",
                                        }
                                    if tc.get("id"):
                                        tool_calls_raw[idx]["id"] = tc["id"]
                                    if tc.get("function", {}).get("name"):
                                        tool_calls_raw[idx]["name"] = tc["function"][
                                            "name"
                                        ]
                                    tool_calls_raw[idx]["args_str"] += tc.get(
                                        "function", {}
                                    ).get("arguments", "")
                        except Exception:
                            pass

                if full_content or tool_calls_raw:
                    msg = {"role": "assistant"}
                    if full_content:
                        msg["content"] = full_content
                    if tool_calls_raw:
                        msg["tool_calls"] = [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": tc["args_str"],
                                },
                            }
                            for tc in tool_calls_raw.values()
                        ]
                    messages.append(msg)

                if not tool_calls_raw:
                    if on_complete:
                        await on_complete(None)
                    return

                for tc in tool_calls_raw.values():
                    tool_call_count += 1
                    try:
                        args = json.loads(tc["args_str"] or "{}")
                    except Exception:
                        args = {}

                    name = tc["name"]
                    print(f"[TRADING] [TOOL] {name}({json.dumps(args)[:300]})")
                    if on_tool_call:
                        await on_tool_call(name, "running", args, "")

                    handler_fn = dispatch.get(name)
                    result = handler_fn(args) if handler_fn else f"Unknown tool: {name}"
                    if on_tool_call:
                        await on_tool_call(name, "complete", args, str(result)[:2000])

                    if name == "send_signal":
                        sig = tool_handler.get_last_signal()
                        if on_complete:
                            await on_complete(sig)
                        return

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": str(result),
                        }
                    )

            if on_error:
                await on_error(f"Max tool calls ({GPT_MAX_TOOL_CALLS}) reached")

    except Exception as e:
        if on_error:
            await on_error(f"Agent error: {str(e)}")
