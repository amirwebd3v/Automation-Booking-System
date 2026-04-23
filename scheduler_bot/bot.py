"""
Scheduler Control Bot
─────────────────────
Tiny always-on bot deployed to Render.com (free tier).
Listens for Telegram commands to change the check interval.
Updates the GitHub Gist config so the next GitHub Actions run picks it up.

Commands:
  /interval 30     → Set check interval to 30 minutes
  /interval 60     → Set check interval to 60 minutes
  /status          → Show current config (interval + last run time)
    /book            → Trigger booking workflow immediately
  /help            → Show available commands

Deploy to Render.com:
  - New Web Service → connect this repo
  - Root directory:  scheduler_bot
  - Build command:   pip install -r requirements_bot.txt
  - Start command:   python bot.py
  - Add env vars:    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GIST_TOKEN, GITHUB_GIST_ID
"""

import os
import json
import time
import asyncio
import aiohttp
import requests
from datetime import datetime, timezone


# ── Config ─────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
AUTHORIZED_CHAT = str(os.environ["TELEGRAM_CHAT_ID"])
GITHUB_TOKEN    = os.environ["GIST_TOKEN"]
GIST_ID         = os.environ["GITHUB_GIST_ID"]
GIST_FILENAME   = "sim24_bot_config.json"
GITHUB_REPO_OWNER = os.environ.get("GITHUB_REPO_OWNER", "amirwebd3v")
GITHUB_REPO_NAME  = os.environ.get("GITHUB_REPO_NAME", "Automation-Booking-System")
GITHUB_WORKFLOW_FILE = os.environ.get("GITHUB_WORKFLOW_FILE", "check_data.yml")
GITHUB_WORKFLOW_REF = os.environ.get("GITHUB_WORKFLOW_REF", "main")

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ── Gist helpers ────────────────────────────────────────────────────────────

def load_gist() -> dict:
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    resp = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=headers, timeout=10)
    resp.raise_for_status()
    content = resp.json()["files"][GIST_FILENAME]["content"]
    return json.loads(content)


def save_gist(data: dict) -> bool:
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    payload = {
        "files": {
            GIST_FILENAME: {
                "content": json.dumps(data, indent=2)
            }
        }
    }
    resp = requests.patch(f"https://api.github.com/gists/{GIST_ID}", headers=headers, json=payload, timeout=10)
    if not resp.ok:
        print(f"[BOT] Gist save failed ({resp.status_code}): {resp.text[:200]}")
    return resp.ok


def trigger_workflow_dispatch() -> None:
    """Trigger GitHub Actions workflow_dispatch for immediate booking run."""
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    payload = {"ref": GITHUB_WORKFLOW_REF}
    url = (
        f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/"
        f"actions/workflows/{GITHUB_WORKFLOW_FILE}/dispatches"
    )

    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    resp.raise_for_status()

# ── Telegram helpers ─────────────────────────────────────────────────────────

async def send_message(chat_id: str, text: str):
    async with aiohttp.ClientSession() as session:
        payload = {
            "chat_id":    chat_id,
            "text":       text,
            "parse_mode": "Markdown"
        }
        await session.post(f"{BASE_URL}/sendMessage", json=payload)

# ── Command handlers ─────────────────────────────────────────────────────────

async def handle_message(message: dict):
    chat_id = str(message.get("chat", {}).get("id", ""))
    text    = message.get("text", "").strip()

    # Security: only respond to authorized chat
    if chat_id != AUTHORIZED_CHAT:
        await send_message(chat_id, "⛔ Unauthorized.")
        return

    if text.startswith("/interval"):
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await send_message(chat_id,
                "❌ Usage: `/interval <minutes>`\nExample: `/interval 45`")
            return

        minutes = int(parts[1])
        if minutes < 5:
            await send_message(chat_id, "⚠️ Minimum interval is 5 minutes.")
            return

        state = load_gist()
        state["interval_minutes"] = minutes
        save_gist(state)

        await send_message(chat_id,
            f"✅ *Interval updated!*\n"
            f"New check interval: `{minutes} minutes`\n"
            f"Takes effect on the next GitHub Actions run."
        )

    elif text == "/status":
        state = load_gist()
        interval = state.get("interval_minutes", 30)
        last_run_ts = state.get("last_run_ts", 0)

        if last_run_ts:
            last_run_dt = datetime.fromtimestamp(last_run_ts, tz=timezone.utc)
            last_run_str = last_run_dt.strftime("%Y-%m-%d %H:%M UTC")
            elapsed_min = int((time.time() - last_run_ts) / 60)
            next_run_in = max(0, interval - elapsed_min)
        else:
            last_run_str = "Never"
            next_run_in = 0

        await send_message(chat_id,
            f"📋 *Current Status*\n"
            f"Check interval: `{interval} minutes`\n"
            f"Last run: `{last_run_str}`\n"
            f"Next run in: ~`{next_run_in} minutes`"
        )

    elif text == "/help":
        await send_message(chat_id,
            "🤖 *sim24 Auto Booker — Commands*\n\n"
            "`/interval <minutes>` — Change check interval\n"
            "`/status` — Show current config and last run\n"
            "`/book` — Trigger booking workflow now\n"
            "`/help` — Show this message\n\n"
            "_Default interval: 10 minutes_\n"
            "_Minimum interval: 5 minutes_"
        )

    elif text == "/book":
        try:
            trigger_workflow_dispatch()
            await send_message(chat_id,
                "🚀 *Manual workflow triggered.*\n"
                "Booking pipeline started now via GitHub Actions.\n"
                "You should receive the result shortly."
            )
        except Exception as e:
            print(f"[BOT] Failed to trigger workflow: {e}")
            await send_message(chat_id,
                "❌ *Could not trigger workflow.*\n"
                "Check token permissions or repository settings."
            )

    else:
        # If a CAPTCHA challenge is pending, treat any plain-text reply as the solution.
        if text and not text.startswith("/"):
            state = load_gist()
            if state.get("captcha_pending"):
                state["captcha_reply"] = text
                state["captcha_pending"] = False
                if save_gist(state):
                    await send_message(chat_id,
                        f"✅ *Captcha code submitted:* `{text}`\n"
                        "The booking workflow will pick it up now."
                    )
                else:
                    await send_message(chat_id,
                        "❌ *Failed to save captcha reply to Gist.*\n"
                        "Check the bot logs and verify `GIST_TOKEN` is a classic PAT with the `gist` scope."
                    )
                return
        # Ignore all other unknown messages

# ── Main polling loop ────────────────────────────────────────────────────────

async def poll():
    print("[BOT] Scheduler bot started. Listening for commands...")
    offset = None

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                params = {
                    "timeout":         30,
                    "allowed_updates": ["message"],
                }
                if offset is not None:
                    params["offset"] = offset

                async with session.get(
                    f"{BASE_URL}/getUpdates",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=35)
                ) as resp:
                    data = await resp.json()

                if not data.get("ok"):
                    await asyncio.sleep(5)
                    continue

                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    if "message" in update:
                        await handle_message(update["message"])

            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[BOT] Error: {e}")
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(poll())
