import json
import time
import aiohttp
from .models import FactorEngineResult, PreMarketData, Signal
from .sessions import get_session

OPENAI_URL     = "https://api.openai.com/v1/chat/completions"
MODEL          = "gpt-5.4"
REASONING      = "medium"
COOLDOWN_SECS  = 300
MAX_TOKENS     = 800

SYSTEM_PROMPT = """You are a professional SPY (S&P 500 ETF) day trader with 10 years of screen time.

PHILOSOPHY:
- Only take high-conviction setups where the story is crystal clear
- Never fight the daily trend
- Respect key levels — Opening Range, Previous Day High/Low, VWAP
- CVD reveals if institutions are behind a move
- Session timing matters — Power Hour has the best setups
- Risk management is non-negotiable

OUTPUT FORMAT:
Respond ONLY with valid JSON. No markdown. No extra text.
{
  "signal": "LONG|SHORT|SKIP",
  "confidence": "HIGH|MEDIUM|LOW",
  "entryType": "BREAKOUT|RETEST|REJECTION|STOP_HUNT",
  "entry": number,
  "stopLoss": number,
  "tp1": number,
  "tp2": number,
  "rr": number,
  "narrative": "one sentence market story, max 100 chars",
  "reasoning": "technical detail, max 120 chars",
  "invalidation": "what kills this setup, max 80 chars",
  "sizeNote": "position sizing advice, max 60 chars",
  "keyRisk": "main risk, max 60 chars"
}

SKIP when: RR < 2.0, conflicting factors, unclear story, dead zone with marginal score.
HIGH confidence only when 3+ strong confirmations with clear levels and no conflicts."""


class SignalEngine:

    def __init__(self, api_key: str):
        self._key          = api_key
        self._last_signal  = 0.0
        self._generating   = False

    def _build_user_prompt(
        self,
        engine: FactorEngineResult,
        pm: PreMarketData | None,
    ) -> str:
        sess = get_session()
        bias = engine.bias.value
        res  = engine.near_resistance
        sup  = engine.near_support

        levels_str = " | ".join(filter(None, [
            f"RES:{res.label}@${res.price:.2f}" if res else "",
            f"SUP:{sup.label}@${sup.price:.2f}" if sup else "",
            f"ORH:${engine.or_data.high:.2f} ORL:${engine.or_data.low:.2f}" if engine.or_data and engine.or_data.complete else "OR:forming",
            f"VWAP:${engine.vwap:.2f}",
            f"PDH:${pm.pd_high:.2f} PDL:${pm.pd_low:.2f}" if pm else "",
        ]))

        factors_str = " | ".join(
            f"{f.label}:{'OK' if f.ok else 'FAIL'}({f.val})"
            for f in engine.factors
        )

        warnings = " ".join(filter(None, [
            "DEAD ZONE: exceptional setup only" if sess.id.value == "DEAD" else "",
            "POWER CLOSE: TP1 only, no holds" if sess.id.value == "CLOSE" else "",
            f"GAP {pm.gap_type} {pm.gap_pct:+.2f}%" if pm else "",
        ]))

        import pytz
        from datetime import datetime
        et_time = datetime.now(pytz.timezone("America/New_York")).strftime("%H:%M:%S")

        return f"""SPY SIGNAL — {et_time} ET

PRICE: ${engine.last_price:.2f} | BIAS: {bias} | SESSION: {sess.label}
SCORE: {engine.total_score}/9 (Context:{engine.context_score}/3 Setup:{engine.setup_score}/4 Timing:{engine.timing_score}/2)
ATR: ${engine.atr:.3f} | CALC_RR: {engine.rr}:1

LEVELS: {levels_str}
FACTORS: {factors_str}

PRICE ACTION: {f"{engine.price_action.type} at {engine.price_action.level.label} ${engine.price_action.level.price:.2f} ({engine.price_action.strength})" if engine.price_action else "None at level"}
STOP HUNT: {f"{engine.stop_hunt.type} sweep at ${engine.stop_hunt.level:.2f} (wick ${engine.stop_hunt.wick_size:.3f})" if engine.stop_hunt else "None"}
{warnings}

Analyze as a professional SPY trader. Is this a genuine institutional move?
Entry should be realistic. SL must be below/above a meaningful level. TP1 at next key level."""

    async def generate(
        self,
        engine: FactorEngineResult,
        pm: PreMarketData | None,
        on_token,
        on_complete,
        on_error,
    ):
        if self._generating:
            return
        if time.time() - self._last_signal < COOLDOWN_SECS:
            return

        self._generating  = True
        self._last_signal = time.time()

        user_prompt = self._build_user_prompt(engine, pm)
        accumulated = ""

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    OPENAI_URL,
                    headers={
                        "Authorization": f"Bearer {self._key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": MODEL,
                        "reasoning_effort": REASONING,
                        "stream": True,
                        "max_tokens": MAX_TOKENS,
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user",   "content": user_prompt},
                        ],
                        "response_format": {"type": "json_object"},
                    },
                ) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        await on_error(f"OpenAI {resp.status}: {err}")
                        return

                    async for line in resp.content:
                        line = line.decode().strip()
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            token = chunk["choices"][0]["delta"].get("content", "")
                            if token:
                                accumulated += token
                                await on_token(token)
                        except:
                            pass

            sig = self._parse(accumulated)
            await on_complete(sig)

        except Exception as e:
            await on_error(str(e))
        finally:
            self._generating = False

    def _parse(self, raw: str) -> Signal:
        try:
            clean  = raw.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(clean)
            return Signal(
                direction   = parsed.get("signal", "SKIP"),
                confidence  = parsed.get("confidence", "LOW"),
                entry_type  = parsed.get("entryType", "BREAKOUT"),
                entry       = float(parsed.get("entry", 0)),
                stop_loss   = float(parsed.get("stopLoss", 0)),
                tp1         = float(parsed.get("tp1", 0)),
                tp2         = float(parsed.get("tp2", 0)),
                rr          = float(parsed.get("rr", 0)),
                narrative   = parsed.get("narrative", ""),
                reasoning   = parsed.get("reasoning", ""),
                invalidation= parsed.get("invalidation", ""),
                size_note   = parsed.get("sizeNote", ""),
                key_risk    = parsed.get("keyRisk", ""),
                generated_at= int(time.time() * 1000),
                stream_complete=True,
            )
        except:
            return Signal(
                direction="SKIP", confidence="LOW", entry_type="BREAKOUT",
                entry=0, stop_loss=0, tp1=0, tp2=0, rr=0,
                narrative="", reasoning="Failed to parse signal",
                invalidation="", size_note="", key_risk="",
                generated_at=int(time.time() * 1000), stream_complete=True,
            )

    def force_reset_cooldown(self):
        self._last_signal = 0.0
