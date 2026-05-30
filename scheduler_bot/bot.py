"""
scheduler_bot/bot.py — Always-on Telegram control bot for sim24 Auto Booker.

Runs as a long-poll process alongside the GitHub Actions pipeline.

Keyboard UX:
  Reply keyboard (persistent bar): [📊 Status]  [📦 Book Now]

  📊 Status  → shows last-run time + captcha flag
               + inline buttons [🔄 Refresh] [📦 Book Now]

  📦 Book Now → dispatches the GitHub Actions check_data.yml workflow

  Captcha reply (plain text while captcha_pending) → saves to Gist

Required environment variables:
  TELEGRAM_BOT_TOKEN   — from @BotFather
  TELEGRAM_CHAT_ID     — your personal chat ID (only authorized user)
  GITHUB_GIST_TOKEN    — Classic PAT with the `gist` scope  (GIST_TOKEN also accepted)
  GITHUB_GIST_ID       — Gist ID                            (GIST_ID   also accepted)
  GITHUB_PAT           — Classic PAT with gist + workflow scopes (for workflow dispatch)
                         Falls back to GITHUB_GIST_TOKEN if not set.
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

# ── Constants ─────────────────────────────────────────────────────────────────

OWNER         = "amirwebd3v"
REPO          = "Automation-Booking-System"
WORKFLOW      = "check_data.yml"
BRANCH        = "main"
GIST_FILENAME = "sim24_bot_config.json"

TG_BASE = "https://api.telegram.org/bot{token}/{method}"

BTN_STATUS = "📊 Status"
BTN_BOOK   = "📦 Book Now"

_REPLY_KEYBOARD: dict = {
    "keyboard": [[{"text": BTN_STATUS}, {"text": BTN_BOOK}]],
    "resize_keyboard": True,
    "persistent": True,
    "one_time_keyboard": False,
}

_STATUS_INLINE: dict = {
    "inline_keyboard": [[
        {"text": "🔄 Refresh",   "callback_data": "refresh"},
        {"text": "📦 Book Now",  "callback_data": "book"},
    ]]
}


# ── Bot ───────────────────────────────────────────────────────────────────────

class Sim24Bot:
    def __init__(self) -> None:
        self.token   = os.environ["TELEGRAM_BOT_TOKEN"]
        self.chat_id = str(os.environ["TELEGRAM_CHAT_ID"])

        # Accept both naming conventions (.env.example uses GIST_TOKEN;
        # the deployed .env may use GITHUB_GIST_TOKEN).
        self.gist_token: str = (
            os.environ.get("GITHUB_GIST_TOKEN")
            or os.environ.get("GIST_TOKEN")
            or ""
        )
        self.gist_id: str = (
            os.environ.get("GITHUB_GIST_ID")
            or os.environ.get("GIST_ID")
            or ""
        )
        # GITHUB_PAT must have gist + workflow scopes for dispatch;
        # falls back to the gist token in case the user combined both scopes there.
        self.gh_pat: str = (
            os.environ.get("GITHUB_PAT")
            or self.gist_token
        )

        self._offset: int = 0
        self._session: aiohttp.ClientSession | None = None

    # ── Low-level Telegram helpers ────────────────────────────────────────────

    def _url(self, method: str) -> str:
        return TG_BASE.format(token=self.token, method=method)

    async def _tg_post(self, method: str, payload: dict) -> dict:
        try:
            async with self._session.post(  # type: ignore[union-attr]
                self._url(method),
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return await resp.json()
        except Exception as exc:
            print(f"[BOT] Telegram {method} failed: {exc}")
            return {}

    async def _send(self, text: str, reply_markup: dict | None = None) -> int | None:
        """Send a Markdown message; returns the new message_id or None."""
        payload: dict = {
            "chat_id":    self.chat_id,
            "text":       text,
            "parse_mode": "Markdown",
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = await self._tg_post("sendMessage", payload)
        return result.get("result", {}).get("message_id")

    async def _edit(self, message_id: int, text: str, reply_markup: dict | None = None) -> None:
        """Edit a message in place; silently ignores 'message is not modified' errors."""
        payload: dict = {
            "chat_id":    self.chat_id,
            "message_id": message_id,
            "text":       text,
            "parse_mode": "Markdown",
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = await self._tg_post("editMessageText", payload)
        if not result.get("ok"):
            desc = result.get("description", "")
            if "not modified" not in desc:
                print(f"[BOT] editMessageText error: {desc}")

    async def _answer_cb(self, callback_id: str, text: str = "") -> None:
        """Acknowledge a button tap to stop the loading spinner."""
        await self._tg_post("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text":              text,
        })

    # ── Gist helpers ──────────────────────────────────────────────────────────

    def _gist_headers(self) -> dict:
        return {
            "Authorization":        f"Bearer {self.gist_token}",
            "Accept":               "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent":           f"{OWNER}/{REPO}-py-bot",
        }

    async def _read_gist(self) -> dict | None:
        try:
            async with self._session.get(  # type: ignore[union-attr]
                f"https://api.github.com/gists/{self.gist_id}",
                headers=self._gist_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if not resp.ok:
                    print(f"[BOT] Gist read failed: HTTP {resp.status}")
                    return None
                data    = await resp.json()
                content = data["files"][GIST_FILENAME]["content"]
                return json.loads(content)
        except Exception as exc:
            print(f"[BOT] Gist read error: {exc}")
            return None

    async def _write_gist(self, state: dict) -> bool:
        try:
            async with self._session.patch(  # type: ignore[union-attr]
                f"https://api.github.com/gists/{self.gist_id}",
                headers=self._gist_headers(),
                json={"files": {GIST_FILENAME: {"content": json.dumps(state, indent=2)}}},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return resp.ok
        except Exception as exc:
            print(f"[BOT] Gist write error: {exc}")
            return False

    # ── GitHub workflow dispatch ───────────────────────────────────────────────

    async def _trigger_workflow(self) -> bool:
        url = (
            f"https://api.github.com/repos/{OWNER}/{REPO}"
            f"/actions/workflows/{WORKFLOW}/dispatches"
        )
        headers = {
            "Authorization":        f"Bearer {self.gh_pat}",
            "Accept":               "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent":           f"{OWNER}/{REPO}-py-bot",
            "Content-Type":         "application/json",
        }
        try:
            async with self._session.post(  # type: ignore[union-attr]
                url,
                headers=headers,
                json={"ref": BRANCH},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 204:
                    body = await resp.text()
                    print(f"[BOT] Workflow dispatch failed: HTTP {resp.status} — {body}")
                return resp.status == 204
        except Exception as exc:
            print(f"[BOT] Workflow dispatch error: {exc}")
            return False

    # ── Status helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _format_status(state: dict) -> str:
        ts = state.get("last_run_ts", 0)
        if ts:
            dt       = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            last_run = dt.strftime("%Y-%m-%d %H:%M UTC")
        else:
            last_run = "Never"

        captcha = (
            "⚠️ Waiting for your reply"
            if state.get("captcha_pending")
            else "✅ None"
        )
        return (
            "📊 *Bot Status*\n\n"
            f"🕑 *Last Run:* `{last_run}`\n"
            f"🔐 *Captcha:* {captcha}"
        )

    # ── Command / button handlers ─────────────────────────────────────────────

    async def _on_start(self) -> None:
        await self._send(
            "👋 *sim24 Auto Booker*\n\n"
            "Use the buttons below to check status or trigger a booking.\n\n"
            "• *📊 Status* — shows last run time and captcha state\n"
            "• *📦 Book Now* — dispatches the GitHub Actions workflow immediately",
            reply_markup=_REPLY_KEYBOARD,
        )

    async def _on_status(self) -> None:
        state = await self._read_gist()
        if state is None:
            await self._send("❌ Could not read status. Check Gist configuration.")
            return
        await self._send(self._format_status(state), reply_markup=_STATUS_INLINE)

    async def _on_book(self) -> None:
        await self._send("⏳ Triggering GitHub Actions workflow…")
        ok = await self._trigger_workflow()
        if ok:
            await self._send("✅ *Workflow triggered!*\nCheck GitHub Actions for progress.")
        else:
            await self._send(
                "❌ Failed to trigger workflow.\n"
                "Make sure `GITHUB_PAT` has the `workflow` scope."
            )

    async def _on_captcha_reply(self, text: str) -> None:
        """Save a plain-text captcha reply to Gist when a solve is pending."""
        state = await self._read_gist()
        if state is None or not state.get("captcha_pending"):
            return  # nothing waiting — ignore

        state["captcha_reply"]   = text
        state["captcha_pending"] = False
        saved = await self._write_gist(state)
        if saved:
            await self._send(
                f"✅ *Captcha code submitted:* `{text}`\n"
                "The booking workflow will pick it up now."
            )
        else:
            await self._send("❌ Failed to save captcha reply to Gist.")

    async def _on_callback(self, cq: dict) -> None:
        cq_id  = cq["id"]
        data   = cq.get("data", "")
        msg_id = cq["message"]["message_id"]

        if data == "refresh":
            await self._answer_cb(cq_id, "Refreshing…")
            state = await self._read_gist()
            if state is None:
                await self._send("❌ Could not read status. Check Gist configuration.")
                return
            await self._edit(msg_id, self._format_status(state), _STATUS_INLINE)

        elif data == "book":
            await self._answer_cb(cq_id, "Triggering…")
            ok = await self._trigger_workflow()
            text = (
                "✅ *Workflow triggered!*\nCheck GitHub Actions for progress."
                if ok
                else "❌ Failed to trigger workflow. Check `GITHUB_PAT` scope."
            )
            await self._send(text)

    # ── Update dispatcher ─────────────────────────────────────────────────────

    async def _handle_update(self, update: dict) -> None:
        if "message" in update:
            msg  = update["message"]
            if str(msg.get("chat", {}).get("id", "")) != self.chat_id:
                return  # reject unauthorized chats silently
            text = (msg.get("text") or "").strip()

            if text in ("/start",):
                await self._on_start()
            elif text == BTN_STATUS:
                await self._on_status()
            elif text == BTN_BOOK:
                await self._on_book()
            elif text and not text.startswith("/"):
                # Could be a captcha reply; handler checks Gist before acting
                await self._on_captcha_reply(text)

        elif "callback_query" in update:
            cq = update["callback_query"]
            if str(cq.get("message", {}).get("chat", {}).get("id", "")) != self.chat_id:
                return
            await self._on_callback(cq)

    # ── Long-poll loop ─────────────────────────────────────────────────────────

    async def _skip_pending(self) -> None:
        """Advance the offset past any queued updates so stale commands are not replayed."""
        try:
            async with self._session.get(  # type: ignore[union-attr]
                self._url("getUpdates"),
                params={"offset": -1, "limit": 1},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data    = await resp.json()
                updates = data.get("result", [])
                if updates:
                    self._offset = updates[-1]["update_id"] + 1
        except Exception:
            pass

    async def run(self) -> None:
        await self._skip_pending()
        print("[BOT] Polling started. Press Ctrl+C to stop.")
        while True:
            try:
                params = {
                    "offset":          self._offset,
                    "timeout":         30,
                    "allowed_updates": json.dumps(["message", "callback_query"]),
                }
                async with self._session.get(  # type: ignore[union-attr]
                    self._url("getUpdates"),
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=40),
                ) as resp:
                    data = await resp.json()
                    for update in data.get("result", []):
                        self._offset = update["update_id"] + 1
                        try:
                            await self._handle_update(update)
                        except Exception as exc:
                            print(f"[BOT] Error handling update {update.get('update_id')}: {exc}")
            except asyncio.CancelledError:
                print("[BOT] Stopping.")
                break
            except Exception as exc:
                print(f"[BOT] Poll error: {exc}")
                await asyncio.sleep(5)

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()
        try:
            # Clear any active webhook so long-polling receives updates
            await self._tg_post("deleteWebhook", {"drop_pending_updates": False})
            print("[BOT] Webhook cleared (if any).")
            await self.run()
        finally:
            await self._session.close()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    bot = Sim24Bot()
    asyncio.run(bot.start())


if __name__ == "__main__":
    main()
