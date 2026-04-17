"""One-way Telegram notifications for summaries and error alerts."""

import aiohttp


class TelegramNotifier:
    BASE_URL = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, token: str, chat_id: str):
        self.token   = token
        self.chat_id = str(chat_id)

    def _url(self, method: str) -> str:
        return self.BASE_URL.format(token=self.token, method=method)

    # ── Send plain text message ───────────────────────────────────────────────

    async def send(self, text: str) -> bool:
        """Send a Markdown-formatted message."""
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "chat_id":    self.chat_id,
                    "text":       text,
                    "parse_mode": "Markdown"
                }
                async with session.post(self._url("sendMessage"), json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    return data.get("ok", False)
        except Exception as e:
            print(f"[TELEGRAM] Send failed: {e}")
            return False

    async def send_photo(self, image_bytes: bytes, caption: str = "") -> bool:
        """Send an image file for error diagnostics."""
        try:
            async with aiohttp.ClientSession() as session:
                form = aiohttp.FormData()
                form.add_field("chat_id", self.chat_id)
                form.add_field("caption", caption)
                form.add_field("parse_mode", "Markdown")
                form.add_field(
                    "photo",
                    image_bytes,
                    filename="captcha.png",
                    content_type="image/png"
                )
                async with session.post(self._url("sendPhoto"), data=form, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    data = await resp.json()
                    return data.get("ok", False)
        except Exception as e:
            print(f"[TELEGRAM] Send photo failed: {e}")
            return False
