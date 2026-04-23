"""Telegram notifications and reply polling for the sim24 bot."""

import asyncio
import time
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
                    if not data.get("ok", False):
                        print(f"[TELEGRAM] sendMessage failed: {data.get('description', data)}")
                    return data.get("ok", False)
        except Exception as e:
            print(f"[TELEGRAM] Send failed: {e}")
            return False

    async def send_photo(self, image_bytes: bytes, caption: str = "") -> bool:
        """Send an image file for error diagnostics."""
        # Telegram enforces a 1024-character limit on photo captions.
        if len(caption) > 1024:
            caption = caption[:1021] + "…"
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
                    if not data.get("ok", False):
                        print(f"[TELEGRAM] sendPhoto failed: {data.get('description', data)}")
                    return data.get("ok", False)
        except Exception as e:
            print(f"[TELEGRAM] Send photo failed: {e}")
            return False

    # ── Receive a reply ───────────────────────────────────────────────────────

    async def wait_for_reply(self, timeout_seconds: int = 300, poll_interval: int = 3) -> "str | None":
        """
        Poll getUpdates directly and return the first text message received from
        the authorised chat after this call, within *timeout_seconds*.

        Strategy:
          1. Drain any already-queued updates (get current offset) so we only
             see messages that arrive *after* the captcha photo was sent.
          2. Long-poll in a loop until a non-command text arrives or we time out.
        """
        # Step 1: drain pending updates to establish a fresh offset.
        offset = await self._drain_updates()

        deadline = time.monotonic() + timeout_seconds
        print(f"[TELEGRAM] Waiting for reply (offset={offset}, timeout={timeout_seconds}s)...")

        async with aiohttp.ClientSession() as session:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                long_poll = min(poll_interval, int(remaining))
                if long_poll <= 0:
                    break

                params: dict = {
                    "timeout":         long_poll,
                    "allowed_updates": ["message"],
                }
                if offset is not None:
                    params["offset"] = offset

                try:
                    async with session.get(
                        self._url("getUpdates"),
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=long_poll + 5),
                    ) as resp:
                        data = await resp.json()
                except Exception as exc:
                    print(f"[TELEGRAM] getUpdates error: {exc}")
                    await asyncio.sleep(2)
                    continue

                if not data.get("ok"):
                    await asyncio.sleep(2)
                    continue

                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    text = (msg.get("text") or msg.get("caption") or "").strip()

                    # Only accept messages from the configured chat
                    if chat_id != self.chat_id:
                        continue
                    # Ignore bot commands
                    if text.startswith("/"):
                        continue
                    if text:
                        print(f"[TELEGRAM] Reply received: {text!r}")
                        return text

        print("[TELEGRAM] wait_for_reply timed out.")
        return None

    async def _drain_updates(self) -> "int | None":
        """Acknowledge all pending updates and return the next expected offset."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self._url("getUpdates"),
                    params={"timeout": 0, "allowed_updates": ["message"]},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
            results = data.get("result", [])
            if not results:
                return None
            last_id = results[-1]["update_id"]
            # Acknowledge by calling with offset = last_id + 1
            async with aiohttp.ClientSession() as session:
                await session.get(
                    self._url("getUpdates"),
                    params={"offset": last_id + 1, "timeout": 0, "allowed_updates": ["message"]},
                    timeout=aiohttp.ClientTimeout(total=10),
                )
            return last_id + 1
        except Exception as exc:
            print(f"[TELEGRAM] _drain_updates error: {exc}")
            return None
