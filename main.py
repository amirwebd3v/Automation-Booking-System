"""
sim24 Auto Data Booker — Main Entry Point
Runs on GitHub Actions every 30 minutes (controlled by the cron schedule).
"""

import sys
import time
import asyncio
from sim24_bot.config_manager import ConfigManager
from sim24_bot.telegram_notify import TelegramNotifier
from sim24_bot.login import Sim24Login
from sim24_bot.data_checker import DataChecker
from sim24_bot.decision_engine import DecisionEngine
from sim24_bot.booking import BookingModule


async def main():
    config = ConfigManager()
    telegram = TelegramNotifier(
        token=config.telegram_token,
        chat_id=config.telegram_chat_id
    )

    print("[START] Starting data check cycle.")

    # ── Interval gate: only run full pipeline when enough time has elapsed ──
    if not config.is_time_to_run():
        elapsed = int((time.time() - config.last_run_ts) / 60)
        print(f"[CONFIG] Skipping — interval not elapsed. "
              f"Elapsed: {elapsed} min / {config.interval_minutes} min required.")
        return

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
        engine = DecisionEngine(threshold_gb=0.5)
        should_book = engine.should_book(remaining_gb)

        status_msg = (
            f"📊 *Data Usage Report*\n"
            f"Used: `{used_gb:.2f} GB` / `{total_gb:.2f} GB`\n"
            f"Remaining: `{remaining_gb:.2f} GB`\n"
            f"Threshold: `{engine.threshold_gb} GB`\n"
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
