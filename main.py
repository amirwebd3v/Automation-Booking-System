"""sim24 Auto Data Booker main entry point."""

import time
import asyncio
from config_manager import ConfigManager
from telegram_notify import TelegramNotifier
from login import Sim24Login
from data_checker import DataChecker
from decision_engine import DecisionEngine
from booking import BookingModule
from captcha_handler import CaptchaSolveError


def _build_run_summary(
    used_gb: float,
    remaining_gb: float,
    total_gb: float,
    should_book: bool,
    booking_success: bool | None,
) -> str:
    if not should_book:
        action = "No action needed."
    elif booking_success:
        action = "✅ 2 GB packet booked successfully."
    else:
        action = "❌ Booking did not complete — see error details above."

    return (
        "✅ *Run complete.*\n"
        f"Used: `{used_gb:.2f} GB` / Total: `{total_gb:.2f} GB`\n"
        f"Remaining: `{remaining_gb:.2f} GB`\n"
        f"Action: {action}"
    )


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

    # ── Interval gate: only run full pipeline when enough time has elapsed ──
    if not config.is_time_to_run():
        elapsed = int((time.time() - config.last_run_ts) / 60)
        print(f"[CONFIG] Skipping — interval not elapsed. "
              f"Elapsed: {elapsed} min / {config.interval_minutes} min required.")
        return

    browser = None
    page = None
    login_module = None
    try:
        login_module = Sim24Login(
            username=config.sim24_username,
            password=config.sim24_password,
            telegram=telegram,
        )
        browser, page = await login_module.login()

        if page is None:
            await telegram.send("❌ *Login failed.* Check credentials or site availability.")
            return

        print("[LOGIN] Success.")

        checker = DataChecker(page)
        used_kb, total_kb = await checker.get_usage()

        if used_kb is None:
            await telegram.send("❌ *Could not read data usage.* Page structure may have changed.")
            return

        used_gb  = used_kb  / (1024 * 1024)
        total_gb = total_kb / (1024 * 1024)
        remaining_gb = total_gb - used_gb

        print(f"[DATA] Used: {used_gb:.2f} GB / {total_gb:.2f} GB | Remaining: {remaining_gb:.2f} GB")

        engine = DecisionEngine(threshold_gb=0.5)
        should_book = engine.should_book(remaining_gb)
        booking_success = None
        if should_book:
            booker = BookingModule(page, telegram)
            booking_success = await booker.book_2gb_packet()

        await telegram.send(
            _build_run_summary(
                used_gb=used_gb,
                remaining_gb=remaining_gb,
                total_gb=total_gb,
                should_book=should_book,
                booking_success=booking_success,
            )
        )

    except CaptchaSolveError as e:
        print(f"[CAPTCHA] {e}")
        await _send_error_alert(
            telegram,
            f"❌ *Gemini failed to solve the CAPTCHA after 3 attempts.*\n`{str(e)}`",
            page,
        )
    except Exception as e:
        error_msg = f"💥 *Unexpected error:*\n`{str(e)}`"
        print(f"[ERROR] {e}")
        await _send_error_alert(telegram, error_msg, page)

    finally:
        config.update_last_run()
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
