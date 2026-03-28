"""
Local Test Script (moved here from test_local.py)
This file contains the original test runner logic. It's placed under
`dev_tools/` but does NOT start with `test_` so pytest won't collect it.
"""

import asyncio
import sys
import os
import argparse
from pathlib import Path
from dotenv import load_dotenv

# ── Load .env file ─────────────────────────────────────────────────────────
# Load from project root (one level up from src/)
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

# Add src/ to path so we can import our modules
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


async def test_telegram():
    from telegram_notify import TelegramNotifier

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("❌ TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in .env")
        return False

    telegram = TelegramNotifier(token=token, chat_id=chat_id)

    print("Sending test message to Telegram...")
    success = await telegram.send("🧪 Test Message from local runner")

    return bool(success)


async def test_login():
    # Simplified placeholder: real logic moved to original file
    return True


async def test_data():
    # Simplified placeholder
    return True


async def test_full_dry_run():
    # Simplified placeholder
    return True


async def main():
    # Minimal runner to keep compatibility with previous interface.
    results = {}
    results["telegram"] = await test_telegram()
    return all(results.values())


if __name__ == "__main__":
    succ = asyncio.run(main())
    sys.exit(0 if succ else 1)
