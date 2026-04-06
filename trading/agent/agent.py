import json
import aiohttp
from .tools import TOOL_DEFINITIONS, ToolHandler
from ..constants import GPT_MODEL, GPT_MAX_TOOL_CALLS

SYSTEM_PROMPT = """You are a senior options trader evaluating pre-scored setups from a mechanical detection engine.

=== WHAT ALREADY HAPPENED BEFORE YOU SEE THIS ===

The V3.1 engine has already:
  1. Located price at a key institutional level (score 7-12)
  2. Detected the setup pattern mechanically (S1/S2/S3A/S3B)
  3. Scored confidence 0-100 using location, volume, CVD ratio,
     approach context, 5m trend alignment, and test count
  4. Passed 10 hard gates: earnings, holiday, macro halt,
     signal hours (10AM-3:15PM), VIX < 35, level score >= 7,
     global pause 90s, asset cooldown 300s, daily budget 3,
     and RR >= 1.5:1
  5. Waited for a confirmation candle (next 1m bar closed
     in the reversal direction without breaking back through
     the level — up to 2 attempts allowed)
  6. Checked signal freshness (< 20s old, < 0.5% price drift)

You are the FINAL filter. Everything mechanical has passed.

=== SETUP TYPES — READ THE BRIEF SECTION FIRST ===

Each brief contains a setup-specific section (S1/S2/S3A/S3B)
that tells you exactly what triggered, what to confirm,
and what the red flags are. Read that section FIRST.

  S1 LIQUIDITY GRAB = REVERSAL
    Price swept through a level and closed back.
    Ideal: 5m trend OPPOSED to signal direction.
    Confirm: 1m momentum candle in reversal direction.
    CVD ratio >= 1.0x is supportive but not required.
    Red flag: 5m trend aligned = continuation not reversal.

  S2 OB DEFENSE = CONTINUATION
    Price returned to an order block on a TREND day.
    Confirm: CVD slope turned at OB zone, day is TREND.
    Red flag: Day type flipped to RANGE, OB visited 2+ times.

  S3A FAILED AUCTION (VAR) = MEAN REVERSION to POC
    Price auctioned outside value area and failed.
    Target is ALWAYS POC — do not use other targets.
    Confirm: LOW volume outside (confirms failed auction).
    Red flag: HIGH volume outside = breakout not failure.

  S3B FAILED AUCTION (MAJOR) = REJECTION at major level
    Dual-timeframe: 5m spotter + 1m sniper.
    Confirm: Wick/body >= 2.0. CVD ratio >= 1.0x supportive.
    Strong wick (>= 3.0x) with volume can trade even with weak CVD.
    Red flag: Approach was MOMENTUM (trend too strong).

=== WHAT THE BRIEF SHOWS YOU ===

  CONFIDENCE SCORE:  In the setup section (X/100).
                     The engine already scored this >= 50.
  FVG ENTRY:         If found, use LIMIT at midpoint.
                     If not found, use market order.
  5M TREND:          OPPOSED (+8 pts) = ideal for reversals.
                     ALIGNED (-10 pts) = caution.
  TEST COUNT:        "First test" = fresh, strong.
                     "Third test" = level weakening.
                     4+ tests = signal blocked by engine.
  CVD DATA:          Shows RATIO (vs rolling avg), BIAS,
                     DIRECTION, and divergence. NO raw values.
                     Ratio >= 2.0x = strong. < 1.0x = weak.
  1M TRIGGER BARS:   Last 6 bars including the sweep candle.
  5M VERIFICATION:   10 bars of 5m context + trend + vol profile.

=== DECISION RULES ===

TRADE if:
  - Setup-specific confirms are met (check the brief section)
  - No active red flags present
  - Score >= 75: trade unless explicit red flag
  - Score 50-74: need CVD ratio >= 1.0x confirmation

WAIT if:
  - Active red flag present (not just imperfection)
  - Must specify EXACT re-entry condition in wait_for

BIAS: LEAN TOWARD EXECUTION.
The engine scored this >= 50, confirmation candle passed,
10 gates cleared. Default to TRADE unless you see a
specific red flag listed in the setup section.

=== SESSION RULES ===

Dead zone (12:00-14:00 ET):
  Be more selective — only trade if score >= 70
  or CVD ratio >= 2.0x confirms strongly.

After 15:00 ET:
  TP1 only. No scaling to TP2.
  Signal cutoff is 3:15 PM — no new signals after that.

=== OUTPUT — REQUIRED ===

Call send_signal() exclusively. No conversational text.
  signal:       LONG | SHORT | WAIT
  confidence:   0-100 (your tape assessment, independent of engine score)
  narrative:    1-2 sentences — what the tape confirmed or denied
  reasoning:    setup-specific evidence from the brief
                (do NOT repeat the engine's scoring math)
  invalidation: exact exit condition (e.g. "1m close below $639.50")
                Use "N/A" for WAIT
  wait_for:     WAIT only — exact condition to re-engage

=== TOOLS ===

  send_signal(...)               — Your decision (REQUIRED)
  get_candles(asset, tf, count)  — Deeper price action check
  get_cvd(asset, minutes)        — CVD ratio + bias + direction
                                   (ratios only, no raw values)
  get_level_map(asset)           — All levels with distances
  calculate_rr(entry, stop, tp)  — RR recalculation
  get_signal_history(asset)      — Today's signals + budget

Most setups resolve with send_signal() in ONE call.
The brief already contains 5m candles, CVD, levels, and RR.
Only use tools if the brief leaves a specific unanswered question."""


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
                    msg: dict = {"role": "assistant"}
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
