"""
Local Test Script
─────────────────
Run this on your local machine BEFORE deploying to GitHub Actions.
Tests each module independently so you can catch issues early.

Usage:
  cd src
  python ../test_local.py

  Or test a specific module:
  python ../test_local.py --test telegram
  python ../test_local.py --test login
  python ../test_local.py --test data
  python ../test_local.py --test full
"""

import asyncio
import sys
import os
import argparse
from pathlib import Path
from dotenv import load_dotenv

# ── Load .env file ─────────────────────────────────────────────────────────
# Load from project root (one level up from src/)
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)

# Add src/ to path so we can import our modules
sys.path.insert(0, str(Path(__file__).parent / "src"))


# ══════════════════════════════════════════════════════════════════════════════
# TEST 1 — Telegram connectivity
# ══════════════════════════════════════════════════════════════════════════════

async def test_telegram():
    print("\n" + "═"*50)
    print("TEST 1: Telegram Bot Connectivity")
    print("═"*50)

    from telegram_notify import TelegramNotifier

    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("❌ TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in .env")
        return False

    telegram = TelegramNotifier(token=token, chat_id=chat_id)

    print("Sending test message to Telegram...")
    success = await telegram.send(
        "🧪 *Test Message*\n"
        "sim24 bot is alive and connected!\n"
        "If you see this, Telegram is working correctly."
    )

    if success:
        print("✅ Telegram message sent successfully!")
    else:
        print("❌ Failed to send Telegram message. Check your token and chat ID.")

    return success


# ══════════════════════════════════════════════════════════════════════════════
# TEST 2 — Login module
# ══════════════════════════════════════════════════════════════════════════════

async def test_login():
    print("\n" + "═"*50)
    print("TEST 2: sim24 Login")
    print("═"*50)

    from telegram_notify import TelegramNotifier
    from login import Sim24Login

    token    = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id  = os.environ.get("TELEGRAM_CHAT_ID")
    username = os.environ.get("SIM24_USERNAME")
    password = os.environ.get("SIM24_PASSWORD")

    if not all([token, chat_id, username, password]):
        print("❌ Missing credentials in .env file.")
        return False

    telegram = TelegramNotifier(token=token, chat_id=chat_id)
    login    = Sim24Login(username=username, password=password, telegram=telegram)

    print(f"Attempting login as: {username}")
    print("(A browser window will open in headless mode — check Telegram if captcha appears)")

    browser, page = await login.login()

    if browser and page:
        print(f"✅ Login successful! Current URL: {page.url}")
        await telegram.send("✅ *Login test passed!* Successfully logged into sim24.")
        await browser.close()
        return True
    else:
        print("❌ Login failed. Check your username/password.")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# TEST 3 — Data checker (requires successful login)
# ══════════════════════════════════════════════════════════════════════════════

async def test_data():
    print("\n" + "═"*50)
    print("TEST 3: Data Usage Reader")
    print("═"*50)

    from telegram_notify import TelegramNotifier
    from login import Sim24Login
    from data_checker import DataChecker
    from decision_engine import DecisionEngine

    token    = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id  = os.environ.get("TELEGRAM_CHAT_ID")
    username = os.environ.get("SIM24_USERNAME")
    password = os.environ.get("SIM24_PASSWORD")

    telegram = TelegramNotifier(token=token, chat_id=chat_id)
    login    = Sim24Login(username=username, password=password, telegram=telegram)

    print("Logging in first...")
    browser, page = await login.login()

    if not browser:
        print("❌ Login failed — cannot test data checker.")
        return False

    print("Reading data usage...")
    checker = DataChecker(page)
    used_kb, total_kb = await checker.get_usage()

    if used_kb is None:
        print("❌ Could not read data usage. Page structure may have changed.")
        await browser.close()
        return False

    used_gb      = used_kb  / (1024 * 1024)
    total_gb     = total_kb / (1024 * 1024)
    remaining_gb = total_gb - used_gb

    print(f"✅ Data usage read successfully:")
    print(f"   Used:      {used_gb:.2f} GB")
    print(f"   Total:     {total_gb:.2f} GB")
    print(f"   Remaining: {remaining_gb:.2f} GB")

    engine      = DecisionEngine(threshold_gb=0.5)
    should_book = engine.should_book(remaining_gb)

    print(f"   Would book? {'YES 🟡' if should_book else 'NO ✅'}")

    await telegram.send(
        f"🧪 *Data Test Passed!*\n"
        f"Used: `{used_gb:.2f} GB` / `{total_gb:.2f} GB`\n"
        f"Remaining: `{remaining_gb:.2f} GB`\n"
        f"Would book: `{'Yes' if should_book else 'No'}`"
    )

    await browser.close()
    return True


# ══════════════════════════════════════════════════════════════════════════════
# TEST 4 — Full dry run (no actual booking)
# ══════════════════════════════════════════════════════════════════════════════

async def test_full_dry_run():
    print("\n" + "═"*50)
    print("TEST 4: Full Dry Run (booking is SIMULATED, not real)")
    print("═"*50)
    print("⚠️  This runs the full pipeline but will NOT click the booking button.")

    from telegram_notify import TelegramNotifier
    from login import Sim24Login
    from data_checker import DataChecker
    from decision_engine import DecisionEngine

    token    = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id  = os.environ.get("TELEGRAM_CHAT_ID")
    username = os.environ.get("SIM24_USERNAME")
    password = os.environ.get("SIM24_PASSWORD")

    telegram = TelegramNotifier(token=token, chat_id=chat_id)
    login    = Sim24Login(username=username, password=password, telegram=telegram)

    await telegram.send("🧪 *Full dry run started...*")

    browser, page = await login.login()
    if not browser:
        print("❌ Login failed.")
        return False

    checker      = DataChecker(page)
    used_kb, total_kb = await checker.get_usage()

    if used_kb is None:
        print("❌ Data read failed.")
        await browser.close()
        return False

    used_gb      = used_kb  / (1024 * 1024)
    total_gb     = total_kb / (1024 * 1024)
    remaining_gb = total_gb - used_gb

    engine      = DecisionEngine(threshold_gb=0.5)
    should_book = engine.should_book(remaining_gb)

    status_msg = (
        f"📊 *Dry Run Complete*\n"
        f"Used: `{used_gb:.2f} GB` / `{total_gb:.2f} GB`\n"
        f"Remaining: `{remaining_gb:.2f} GB`\n"
        f"Would trigger booking: `{'YES' if should_book else 'NO'}`\n\n"
        f"_(Booking was NOT executed in dry run mode)_"
    )

    print("\n" + status_msg.replace("*", "").replace("`", ""))
    await telegram.send(status_msg)
    await browser.close()

    print("\n✅ Full dry run completed successfully!")
    return True


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    parser = argparse.ArgumentParser(description="sim24 Bot Local Test Suite")
    parser.add_argument(
        "--test",
        choices=["telegram", "login", "data", "full"],
        default="telegram",
        help="Which test to run (default: telegram)"
    )
    args = parser.parse_args()

    results = {}

    if args.test == "telegram":
        results["telegram"] = await test_telegram()

    elif args.test == "login":
        results["telegram"] = await test_telegram()
        if results["telegram"]:
            results["login"] = await test_login()

    elif args.test == "data":
        results["telegram"] = await test_telegram()
        if results["telegram"]:
            results["login"]   = await test_login()
            if results.get("login"):
                results["data"] = await test_data()

    elif args.test == "full":
        results["telegram"] = await test_telegram()
        if results["telegram"]:
            results["full"] = await test_full_dry_run()

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "═"*50)
    print("RESULTS SUMMARY")
    print("═"*50)
    for name, passed in results.items():
        icon = "✅" if passed else "❌"
        print(f"  {icon} {name.capitalize()}")

    all_passed = all(results.values())
    print(f"\n{'✅ All tests passed!' if all_passed else '❌ Some tests failed — check output above.'}")
    return all_passed


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
