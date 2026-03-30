import json
import aiohttp
from .tools import TOOL_DEFINITIONS, ToolHandler
from ..constants import GPT_MODEL, GPT_MAX_TOOL_CALLS

SYSTEM_PROMPT = """You are the Chief Risk Officer for an institutional options day trading desk.
Assets: SPY, QQQ, AAPL, NVDA, TSLA, MSFT, META, AMZN.

A mechanical detection engine flagged a setup and passed it to you via a Brief + Verification Data.
The Gate System has ALREADY verified: time of day, VIX limits, daily budget, level score minimum.
Do NOT re-check these. Your sole job: verify price action, structure, and order flow. Protect capital.

=== EVALUATION PROTOCOL ===

STEP 1 — FAST-FAIL (Veto immediately if ANY are true):
  - Pre-computed RR < 1.5:1 and no logical alternative target exists (2.0:1 preferred, 1.5:1 acceptable for score 8+ with strong confluence)
  - 5m trend strongly opposes trade direction AND level score < 9
  - 5m candle wicks show repeated rejection against the intended direction
  - CVD is diverging violently against the claimed pattern (price up + CVD collapsing = fake)
  → If any veto triggers: call send_signal(WAIT) with wait_for explaining what would change your mind

STEP 2 — DEVIL'S ADVOCATE (Required before any LONG/SHORT):
  Formulate the strongest argument for why this trade FAILS:
  - Is CVD delta opposing price? (Neutral CVD is acceptable; diverging CVD is a red flag)
  - Are we buying into resistance (HVN/VAH) or selling into support (VAL/POC)?
  - Does the 5m tape actually confirm the pattern, or is price choppy/fading?
  - Counter-trend trade with score < 9? Likely a trap.
  Write this argument down in your reasoning.

STEP 3 — VERDICT:
  If confluence of trend + CVD + structure defeats your Devil's Advocate:
  → LONG or SHORT
  If the Devil's Advocate wins:
  → WAIT with specific re-entry condition

=== OUTPUT RULES ===

Trade fields (entry, stop, TP, options) are AUTO-FILLED from the Brief. You provide only your decision:
  - signal: LONG / SHORT / WAIT
  - confidence: 0-100 integer (80+ = HIGH, 50-79 = MEDIUM, below 50 = LOW)
  - narrative: 1-2 sentence thesis
  - reasoning: why the Devil's Advocate argument failed (LONG/SHORT) or won (WAIT)
  - invalidation: exact exit condition (e.g. "1m close below POC $639.50") or "N/A" for WAIT
  - wait_for: WAIT only — exact condition to re-engage

=== SESSION CONTEXT ===
  - Dead zone (12-2 PM): only execute on score 8+ levels
  - After 3:00 PM: TP1 only, no runners, rapid theta decay
  - Counter-trend: requires score >= 9 and CVD confirmation

=== TOOLS ===
  send_signal(...)               — Final decision (REQUIRED — always call this)
  get_candles(asset, tf, count)  — Deeper dive on 1m/5m/15m/daily bars
  get_cvd(asset, minutes)        — Extended CVD timeline + divergence check
  get_level_info(asset, name)    — Score, confluence, test history for a level
  get_level_map(asset)           — All levels with ATR distances
  calculate_rr(entry, stop, tp)  — Recalculate with different targets
  get_signal_history(asset)      — Signals fired today + budget remaining

Use tools ONLY if Brief + Verification Data leave a critical question unanswered.
Most setups should resolve in one call to send_signal."""


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
    headers = {"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"}

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
                payload = {"model": model, "messages": messages, "tools": TOOL_DEFINITIONS, "tool_choice": "auto", "temperature": 0.2, "stream": True}
                async with session.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        if on_error: await on_error(f"OpenAI {resp.status}: {err[:200]}")
                        return

                    full_content = ""
                    tool_calls_raw = {}
                    async for line in resp.content:
                        line = line.decode().strip()
                        if not line.startswith("data: "): continue
                        data = line[6:]
                        if data == "[DONE]": break
                        try:
                            chunk = json.loads(data)
                            delta = chunk["choices"][0].get("delta", {})
                            if "content" in delta and delta["content"]:
                                full_content += delta["content"]
                                if on_token: await on_token(delta["content"])
                            if "tool_calls" in delta:
                                for tc in delta["tool_calls"]:
                                    idx = tc["index"]
                                    if idx not in tool_calls_raw:
                                        tool_calls_raw[idx] = {"id": "", "name": "", "args_str": ""}
                                    if tc.get("id"): tool_calls_raw[idx]["id"] = tc["id"]
                                    if tc.get("function", {}).get("name"): tool_calls_raw[idx]["name"] = tc["function"]["name"]
                                    tool_calls_raw[idx]["args_str"] += tc.get("function", {}).get("arguments", "")
                        except Exception:
                            pass

                if full_content or tool_calls_raw:
                    msg = {"role": "assistant"}
                    if full_content: msg["content"] = full_content
                    if tool_calls_raw:
                        msg["tool_calls"] = [{"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["args_str"]}} for tc in tool_calls_raw.values()]
                    messages.append(msg)

                if not tool_calls_raw:
                    if on_complete: await on_complete(None)
                    return

                for tc in tool_calls_raw.values():
                    tool_call_count += 1
                    try:
                        args = json.loads(tc["args_str"] or "{}")
                    except Exception:
                        args = {}

                    name = tc["name"]
                    print(f"[TRADING] [TOOL] {name}({json.dumps(args)[:300]})")
                    if on_tool_call: await on_tool_call(name, "running", args, "")

                    handler_fn = dispatch.get(name)
                    result = handler_fn(args) if handler_fn else f"Unknown tool: {name}"
                    if on_tool_call: await on_tool_call(name, "complete", args, str(result)[:2000])

                    if name == "send_signal":
                        sig = tool_handler.get_last_signal()
                        if on_complete: await on_complete(sig)
                        return

                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(result)})

            if on_error: await on_error(f"Max tool calls ({GPT_MAX_TOOL_CALLS}) reached")

    except Exception as e:
        if on_error: await on_error(f"Agent error: {str(e)}")
