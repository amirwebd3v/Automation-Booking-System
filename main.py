"""sim24 Auto Data Booker main entry point."""

import asyncio
import os
from config_manager import ConfigManager
from telegram_notify import TelegramNotifier
from login import Sim24Login
from data_checker import DataChecker
from decision_engine import DecisionEngine
from booking import BookingModule
from captcha_handler import CaptchaSolveError



async def _send_error_alert(telegram: TelegramNotifier, message: str, page=None) -> None:
    screenshot_sent = False
    if page is not None:
        try:
            screenshot = await page.screenshot(full_page=True)
            screenshot_sent = await telegram.send_photo(screenshot, caption=message)
        except Exception as exc:
            print(f"[ERROR] Failed to capture/send screenshot: {exc}")

    if not screenshot_sent:
        await telegram.send(message)


async def main():
    config = ConfigManager()
    telegram = TelegramNotifier(
        token=config.telegram_token,
        chat_id=config.telegram_chat_id
    )

    print("[START] Starting data check cycle.")

    browser = None
    page = None
    login_module = None
    used_kb = None
    total_kb = None
    run_success = False
    error_detail = ""
    try:
        login_module = Sim24Login(
            username=config.sim24_username,
            password=config.sim24_password,
            telegram=telegram,
        )
        browser, page = await login_module.login()

        if page is None:
            error_detail = "Login failed. Check credentials or site availability."
            await telegram.send("❌ *Login failed.* Check credentials or site availability.")
            return

        print("[LOGIN] Success.")

        checker = DataChecker(page)
        used_kb, total_kb = await checker.get_usage()

        if used_kb is None:
            error_detail = "Could not read data usage. Page structure may have changed."
            await telegram.send("❌ *Could not read data usage.* Page structure may have changed.")
            return

        config.record_usage_snapshot(used_kb=used_kb, total_kb=total_kb)

        used_gb  = used_kb  / (1024 * 1024)
        total_gb = total_kb / (1024 * 1024)
        remaining_gb = total_gb - used_gb

        print(f"[DATA] Used: {used_gb:.2f} GB / {total_gb:.2f} GB | Remaining: {remaining_gb:.2f} GB")

        engine = DecisionEngine(threshold_gb=0.5)
        should_book = engine.should_book(remaining_gb)
        force_report = os.environ.get("FORCE_REPORT", "false").lower() == "true"
        booking_success = None
        if should_book:
            booker = BookingModule(page, telegram, config)
            booking_success = await booker.book_2gb_packet()

        run_success = (not should_book) or (booking_success is True)
        if should_book and booking_success is False:
            error_detail = "Booking attempted but did not succeed."

        if should_book and booking_success:
            await telegram.send(
                f"✅ *2 GB packet booked successfully.*\n"
                f"Used: `{used_gb:.2f} GB` / Total: `{total_gb:.2f} GB`"
            )
        elif not should_book and force_report:
            await telegram.send(
                f"📊 *Data status (no booking needed)*\n"
                f"Used: `{used_gb:.2f} GB` / Total: `{total_gb:.2f} GB`\n"
                f"Remaining: `{remaining_gb:.2f} GB` (threshold: 0.5 GB)"
            )

    except CaptchaSolveError as e:
        error_detail = f"CAPTCHA could not be solved after 3 attempts: {str(e)}"
        print(f"[CAPTCHA] {e}")
        await _send_error_alert(
            telegram,
            f"\u274c *CAPTCHA could not be solved after 3 attempts.*\n`{str(e)}`",
            page,
        )
    except Exception as e:
        error_detail = f"Unexpected error: {str(e)}"
        error_msg = f"💥 *Unexpected error:*\n`{str(e)}`"
        print(f"[ERROR] {e}")
        await _send_error_alert(telegram, error_msg, page)

    finally:
        config.record_run(
            success=run_success,
            error=error_detail,
            used_kb=used_kb,
            total_kb=total_kb,
        )
        if page:
            try:
                await page.goto("https://service.sim24.de/public/prelogout", wait_until="load", timeout=15000)
                print("[LOGOUT] Logged out successfully.")
            except Exception as exc:
                print(f"[LOGOUT] Warning: logout request failed: {exc}")
        if browser:
            await browser.close()
        pw = getattr(login_module, "_playwright", None)
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
