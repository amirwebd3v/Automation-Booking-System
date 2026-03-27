"""
Telegram Notifier
─────────────────
Handles all Telegram communication:
  - Sending messages
  - Sending photos (captcha images)
  - Waiting for user reply (for captcha solving)
"""

import asyncio
import aiohttp
from typing import Optional


class TelegramNotifier:
    BASE_URL = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, token: str, chat_id: str):
        self.token   = token
        self.chat_id = str(chat_id)
        self._last_update_id = None

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

    # ── Send photo (captcha image) ────────────────────────────────────────────

    async def send_photo(self, image_bytes: bytes, caption: str = "") -> bool:
        """Send an image file (e.g. captcha screenshot)."""
        try:
            async with aiohttp.ClientSession() as session:
                form = aiohttp.FormData()
                form.add_field("chat_id", self.chat_id)
                form.add_field("caption", caption)
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

    # ── Wait for user reply ───────────────────────────────────────────────────

    async def wait_for_reply(self, timeout_seconds: int = 180) -> Optional[str]:
        """
        Long-polls Telegram for a new message from the user.
        Returns the text of the first reply received, or None on timeout.
        Used for captcha solving: we send the image, then wait here.
        """
        print(f"[TELEGRAM] Waiting up to {timeout_seconds}s for user reply...")
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        poll_timeout = 20  # Telegram long-poll window in seconds

        # Get current update offset to only read NEW messages
        offset = await self._get_latest_update_id()
        if offset is not None:
            offset += 1  # Move past all existing messages

        async with aiohttp.ClientSession() as session:
            while asyncio.get_event_loop().time() < deadline:
                remaining = int(deadline - asyncio.get_event_loop().time())
                wait_time = min(poll_timeout, remaining)
                if wait_time <= 0:
                    break

                try:
                    params = {
                        "timeout":          wait_time,
                        "allowed_updates":  ["message"],
                    }
                    if offset is not None:
                        params["offset"] = offset

                    async with session.get(
                        self._url("getUpdates"),
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=wait_time + 5)
                    ) as resp:
                        data = await resp.json()

                    if not data.get("ok"):
                        continue

                    updates = data.get("result", [])
                    for update in updates:
                        update_id = update.get("update_id", 0)
                        offset = update_id + 1  # Acknowledge this update

                        message = update.get("message", {})
                        sender_id = str(message.get("chat", {}).get("id", ""))
                        text = message.get("text", "").strip()

                        # Only accept messages from the authorized chat
                        if sender_id == self.chat_id and text:
                            print(f"[TELEGRAM] Got reply: {text}")
                            return text

                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    print(f"[TELEGRAM] Poll error: {e}")
                    await asyncio.sleep(2)

        print("[TELEGRAM] Timeout — no reply received.")
        return None

    async def _get_latest_update_id(self) -> Optional[int]:
        """Fetch the latest update_id so we can skip old messages."""
        try:
            async with aiohttp.ClientSession() as session:
                params = {"limit": 1, "offset": -1}
                async with session.get(
                    self._url("getUpdates"),
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    data = await resp.json()
                    updates = data.get("result", [])
                    if updates:
                        return updates[-1]["update_id"]
                    return None
        except Exception:
            return None
