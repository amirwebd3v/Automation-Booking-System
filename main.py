"""
sim24 Auto Data Booker — Main Entry Point
Runs on GitHub Actions every 5 minutes.
Checks elapsed time against configured interval before doing actual work.
"""

import sys
import asyncio
from config_manager import ConfigManager
from telegram_notify import TelegramNotifier
from login import Sim24Login
from data_checker import DataChecker
from decision_engine import DecisionEngine
from booking import BookingModule


async def main():
    config = ConfigManager()
    telegram = TelegramNotifier(
        token=config.telegram_token,
        chat_id=config.telegram_chat_id
    )

    # ── Check if enough time has passed since last run ──────────────────────
    if not config.is_time_to_run():
        print(f"[SKIP] Not time yet. Interval: {config.interval_minutes}min. Exiting.")
        return

    print("[START] Time to run — starting data check cycle.")

    browser = None
    try:
        # ── Step 1: Login ────────────────────────────────────────────────────
        login_module = Sim24Login(
            username=config.sim24_username,
            password=config.sim24_password,
            telegram=telegram          # ← needed for captcha on login page
        )
        browser, page = await login_module.login()

        if page is None:
            await telegram.send("❌ *Login failed.* Check credentials or site availability.")
            config.update_last_run()
            return

        print("[LOGIN] Success.")

        # ── Step 2: Check Data Volume ────────────────────────────────────────
        checker = DataChecker(page)
        used_kb, total_kb = await checker.get_usage()

        if used_kb is None:
            await telegram.send("❌ *Could not read data usage.* Page structure may have changed.")
            config.update_last_run()
            return

        used_gb  = used_kb  / (1024 * 1024)
        total_gb = total_kb / (1024 * 1024)
        remaining_gb = total_gb - used_gb

        print(f"[DATA] Used: {used_gb:.2f} GB / {total_gb:.2f} GB | Remaining: {remaining_gb:.2f} GB")

        # ── Step 3: Decision Engine ──────────────────────────────────────────
        engine = DecisionEngine(threshold_gb=1.5)
        should_book = engine.should_book(remaining_gb)

        status_msg = (
            f"📊 *Data Usage Report*\n"
            f"Used: `{used_gb:.2f} GB` / `{total_gb:.2f} GB`\n"
            f"Remaining: `{remaining_gb:.2f} GB`\n"
            f"Threshold: `1.5 GB`\n"
            f"Action: {'🟡 Booking triggered...' if should_book else '✅ No action needed'}"
        )
        await telegram.send(status_msg)

        # ── Step 4: Book if needed ───────────────────────────────────────────
        if should_book:
            booker = BookingModule(page, telegram)
            success = await booker.book_2gb_packet()

            if success:
                await telegram.send("✅ *2 GB packet booked successfully!*")
            else:
                await telegram.send("❌ *Booking failed.* Manual action may be required.")

        config.update_last_run()

    except Exception as e:
        error_msg = f"💥 *Unexpected error:*\n`{str(e)}`"
        print(f"[ERROR] {e}")
        await telegram.send(error_msg)
        config.update_last_run()

    finally:
        if browser:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
