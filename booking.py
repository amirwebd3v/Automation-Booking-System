"""
Booking Module
──────────────
Handles the 2GB data packet booking flow.

From the HTML analysis:
  Button ID:    ButtonBuchen-ChangeServiceType-showGprsDataUsage-2V5I3
  Form ID:      BaseForm-ChangeServiceType-showGprsDataUsage-2V5I3
  Service code: V5I3

Flow:
  1. Check if the button is present and not disabled
  2. Click it
  3. Handle captcha if it appears
  4. Confirm the booking
  5. Verify success
"""

import asyncio
from playwright.async_api import Page
from telegram_notify import TelegramNotifier
from captcha_handler import CaptchaHandler
from typing import Optional


BOOK_BUTTON_ID = "ButtonBuchen-ChangeServiceType-showGprsDataUsage-2V5I3"
BOOK_FORM_ID   = "BaseForm-ChangeServiceType-showGprsDataUsage-2V5I3"


class BookingModule:
    def __init__(self, page: Page, telegram: TelegramNotifier):
        self.page     = page
        self.telegram = telegram
        self.captcha  = CaptchaHandler(page, telegram)

    async def book_2gb_packet(self) -> bool:
        """
        Full booking flow. Returns True on success, False on failure.
        """
        print("[BOOKING] Starting 2GB packet booking...")

        # ── Step 1: Check button state ─────────────────────────────────────
        button = await self.page.query_selector(f"#{BOOK_BUTTON_ID}")

        if button is None:
            print("[BOOKING] Book button not found on page.")
            await self.telegram.send(
                "⚠️ *Booking button not found.*\n"
                "The page structure may have changed."
            )
            return False

        # Check if button is disabled
        is_disabled = await button.get_attribute("disabled")
        if is_disabled is not None:
            print("[BOOKING] Button is disabled. Attempting JS override...")
            # Try to remove the disabled attribute via JavaScript
            await self.page.evaluate(
                f"document.getElementById('{BOOK_BUTTON_ID}').removeAttribute('disabled')"
            )
            await asyncio.sleep(0.5)

        # ── Step 2: Click the booking button ──────────────────────────────
        print("[BOOKING] Clicking book button...")
        await button.click()
        await asyncio.sleep(2)  # Wait for any modal/captcha to appear

        # ── Step 3: Handle captcha if present ─────────────────────────────
        captcha_present = await self.captcha.is_captcha_present()
        if captcha_present:
            print("[BOOKING] Captcha detected — sending to Telegram.")
            await self.telegram.send(
                "🔐 *Captcha appeared during booking.*\n"
                "Sending image now..."
            )

            solution = await self.captcha.solve()
            if solution is None:
                return False  # Timeout — captcha handler already notified

            # Enter the solution
            entered = await self.captcha.enter_solution(solution)
            if not entered:
                await self.telegram.send(
                    "❌ *Could not enter captcha solution.*\n"
                    "The input field was not found."
                )
                return False

            await asyncio.sleep(0.5)

        # ── Step 4: Look for a confirmation button (submit the booking) ────
        # After solving captcha (or if no captcha), find the confirm/submit
        confirmed = await self._confirm_booking()
        if not confirmed:
            # Maybe the form auto-submitted after captcha — check for success
            pass

        # ── Step 5: Verify booking success ────────────────────────────────
        success = await self._verify_success()
        return success

    async def _confirm_booking(self) -> bool:
        """
        Looks for a confirmation button or form submit after captcha.
        Returns True if found and clicked, False otherwise.
        """
        # Common selectors for confirmation submit buttons
        confirm_selectors = [
            "button[type='submit']",
            "input[type='submit']",
            "a.c-button[title='Buchen']",
            "a.c-button[title='Bestätigen']",
            "a.c-button[title='Bestellen']",
            ".btn-submit",
        ]

        for selector in confirm_selectors:
            try:
                btn = await self.page.query_selector(selector)
                if btn and await btn.is_visible():
                    print(f"[BOOKING] Found confirm button: {selector}")
                    await btn.click()
                    await asyncio.sleep(2)
                    return True
            except Exception:
                continue

        # Try submitting the form directly via JavaScript
        try:
            form_exists = await self.page.query_selector(f"#{BOOK_FORM_ID}")
            if form_exists:
                print("[BOOKING] Submitting form via JavaScript...")
                await self.page.evaluate(f"document.getElementById('{BOOK_FORM_ID}').submit()")
                await asyncio.sleep(2)
                return True
        except Exception as e:
            print(f"[BOOKING] JS form submit failed: {e}")

        return False

    async def _verify_success(self) -> bool:
        """
        Checks for success indicators on the page after booking attempt.
        """
        await asyncio.sleep(2)

        try:
            # Check for success message elements (common German success text)
            success_indicators = [
                "text=erfolgreich",      # "successfully"
                "text=Datenpaket",       # "data package"
                "text=gebucht",          # "booked"
                "text=Buchung",          # "booking"
                ".noticeBox-success",
                ".alert-success",
                ".success",
            ]

            page_content = await self.page.content()
            page_content_lower = page_content.lower()

            success_keywords = ["erfolgreich", "gebucht", "buchung bestätigt", "successfully"]
            for keyword in success_keywords:
                if keyword in page_content_lower:
                    print(f"[BOOKING] Success keyword found: '{keyword}'")
                    return True

            # Also check URL change (some sites redirect to confirmation page)
            current_url = self.page.url
            if "success" in current_url.lower() or "bestaetigung" in current_url.lower():
                return True

        except Exception as e:
            print(f"[BOOKING] Verification check failed: {e}")

        # If we can't confirm, take a screenshot and send for manual verification
        try:
            screenshot = await self.page.screenshot(full_page=False)
            await self.telegram.send_photo(
                image_bytes=screenshot,
                caption=(
                    "📸 *Booking result page screenshot*\n"
                    "Please verify if the booking was successful."
                )
            )
        except Exception:
            pass

        # Ambiguous — treat as potentially successful but notify
        await self.telegram.send(
            "⚠️ *Booking submitted but could not auto-verify.*\n"
            "Please check the screenshot above and your account."
        )
        return True  # Return True to avoid double-booking on next cycle
