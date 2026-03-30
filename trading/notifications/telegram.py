import os
import aiohttp
from .formatter import format_telegram
from ..models import Signal


class TelegramNotifier:
    def __init__(self):
        self._token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self._enabled = bool(self._token and self._chat_id)

    async def send_signal(self, signal: Signal):
        if not self._enabled or signal.direction == "WAIT":
            return
        text = format_telegram(signal)
        if text:
            await self._send(text)

    async def send_text(self, text: str):
        if self._enabled:
            await self._send(text)

    async def _send(self, text: str):
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(url, json={"chat_id": self._chat_id, "text": text, "parse_mode": "Markdown"}, timeout=aiohttp.ClientTimeout(total=10))
        except Exception as e:
            print(f"[Telegram] Send failed: {e}")
