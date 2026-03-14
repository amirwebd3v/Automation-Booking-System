"""
Captcha Handler
───────────────
When the booking form presents a captcha:
  1. Take a focused screenshot of the captcha element
  2. Send it to Telegram with an urgent prompt
  3. Wait up to 3 minutes for the user to reply with the solution
  4. Return the solution text (or None on timeout)

Retry flow (solve_with_retry):
  - Solves captcha interactively via Telegram
  - Clicks the Aktivieren button in the dialog
  - Detects "nicht korrekt" error and retries with a freshly reloaded image
  - Returns True when captcha accepted, False after max attempts or timeout
"""

import asyncio
from playwright.async_api import Page
from telegram_notify import TelegramNotifier
from typing import Optional


# Possible selectors for image-based captcha on the sim24 portal
# These will be verified on first real run and updated if needed
CAPTCHA_SELECTORS = [
    "img[src*='captcha']",
    "img[alt*='captcha']",
    "img[alt*='Captcha']",
    ".captcha img",
    "#captcha img",
    "img[src*='securimage']",
    "img[src*='code']",
]

# Input field where we type the solved captcha text
CAPTCHA_INPUT_SELECTORS = [
    "input[name*='captcha']",
    "input[id*='captcha']",
    "input[name*='code']",
    "#captcha_code",
]

# "Neuen Code anzeigen" reload link inside the captcha dialog
CAPTCHA_RELOAD_SELECTORS = [
    "a.captcha_reload",
    "a[href^='javascript:reload_captcha']",
    ".reload a",
]

# Aktivieren submit button inside the captcha dialog
AKTIVIEREN_SELECTORS = [
    "a[title='Aktivieren']",
    "[id^='ButtonAktivieren-ChangeServiceType-getChangeServiceInfo-']",
    "a.button2FormModal",
]

# Page text fragments that confirm the CAPTCHA answer was rejected
CAPTCHA_ERROR_TEXTS = [
    "der eingegebene code ist nicht korrekt",
    "code ist nicht korrekt",
    "captcha ist nicht korrekt",
    "ungültiger code",
    "ungültiger captcha",
]


class CaptchaHandler:
    def __init__(self, page: Page, telegram: TelegramNotifier):
        self.page     = page
        self.telegram = telegram

    async def is_captcha_present(self) -> bool:
        """Check if a captcha element exists on the current page."""
        for selector in CAPTCHA_SELECTORS:
            try:
                element = await self.page.query_selector(selector)
                if element and await element.is_visible():
                    print(f"[CAPTCHA] Found captcha via selector: {selector}")
                    return True
            except Exception:
                continue
        return False

    async def solve(self) -> Optional[str]:
        """
        Full captcha solving flow:
          1. Find and screenshot the captcha image
          2. Send to Telegram
          3. Wait for user reply
          4. Return the solution string
        """
        captcha_element = None

        # Find the captcha image element
        for selector in CAPTCHA_SELECTORS:
            try:
                el = await self.page.query_selector(selector)
                if el and await el.is_visible():
                    captcha_element = el
                    break
            except Exception:
                continue

        if captcha_element is None:
            # Fallback: screenshot the full visible viewport area
            print("[CAPTCHA] Could not isolate captcha element — using viewport screenshot.")
            screenshot_bytes = await self.page.screenshot(full_page=False)
        else:
            # Screenshot just the captcha element with some padding
            try:
                box = await captcha_element.bounding_box()
                if box:
                    # Add padding around the captcha for readability
                    clip = {
                        "x":      max(0, box["x"] - 20),
                        "y":      max(0, box["y"] - 20),
                        "width":  box["width"]  + 40,
                        "height": box["height"] + 40,
                    }
                    screenshot_bytes = await self.page.screenshot(clip=clip)
                else:
                    screenshot_bytes = await captcha_element.screenshot()
            except Exception:
                screenshot_bytes = await self.page.screenshot(full_page=False)

        # Send to Telegram
        await self.telegram.send_photo(
            image_bytes=screenshot_bytes,
            caption=(
                "🔐 *Captcha Required*\n"
                "Please reply with the letters/numbers shown in the image.\n"
                "⏳ You have *3 minutes* to respond."
            )
        )

        # Wait for reply
        solution = await self.telegram.wait_for_reply(timeout_seconds=180)

        if solution is None:
            await self.telegram.send(
                "⏰ *Captcha timeout.* No reply received in 3 minutes.\n"
                "Booking attempt aborted. Will retry next cycle."
            )

        return solution

    async def enter_solution(self, solution: str) -> bool:
        """Type the solved captcha text into the input field."""
        for selector in CAPTCHA_INPUT_SELECTORS:
            try:
                field = await self.page.query_selector(selector)
                if field and await field.is_visible():
                    await field.fill(solution)
                    print(f"[CAPTCHA] Entered solution in field: {selector}")
                    return True
            except Exception:
                continue

        print("[CAPTCHA] Could not find captcha input field.")
        return False

    async def is_captcha_error(self) -> bool:
        """Return True if the page currently shows a 'wrong captcha' error message."""
        try:
            content = (await self.page.content()).lower()
            return any(err in content for err in CAPTCHA_ERROR_TEXTS)
        except Exception:
            return False

    async def reload_captcha_image(self) -> bool:
        """Click 'Neuen Code anzeigen' to fetch a fresh CAPTCHA image."""
        for sel in CAPTCHA_RELOAD_SELECTORS:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    await asyncio.sleep(1.5)  # Wait for the new image to load
                    print(f"[CAPTCHA] Captcha image reloaded via: {sel}")
                    return True
            except Exception:
                continue
        print("[CAPTCHA] Could not reload captcha image (reload link not found).")
        return False

    async def click_aktivieren(self) -> bool:
        """Click the Aktivieren button inside the captcha dialog to submit the form."""
        for sel in AKTIVIEREN_SELECTORS:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    print(f"[CAPTCHA] Clicked Aktivieren via: {sel}")
                    await asyncio.sleep(3)  # Wait for AJAX response to settle
                    return True
            except Exception:
                continue
        print("[CAPTCHA] Could not find Aktivieren button in dialog.")
        return False

    async def solve_with_retry(self, max_attempts: int = 3) -> bool:
        """
        Interactive Telegram CAPTCHA loop with retry on wrong answer.

        Each attempt:
          1. (On retry) Reload the captcha image so the user sees a fresh code.
          2. Screenshot the captcha and send it to Telegram.
          3. Wait up to 3 minutes for the user's text reply.
          4. Fill the answer into the input field.
          5. Click the Aktivieren button.
          6. Check whether the site rejected the answer ('nicht korrekt').
             - If accepted: return True.
             - If rejected: notify the user and try again.
        Returns False when all attempts are exhausted or the user times out.
        """
        for attempt in range(1, max_attempts + 1):
            print(f"[CAPTCHA] Solve attempt {attempt}/{max_attempts}")

            # Reload captcha on retries so the user doesn't re-solve a stale image
            if attempt > 1:
                await self.reload_captcha_image()

            # Screenshot + send to Telegram + wait for reply
            solution = await self.solve()
            if solution is None:
                # User did not reply in time — no point retrying
                return False

            # Enter the answer
            if not await self.enter_solution(solution):
                print("[CAPTCHA] Could not locate the captcha input field.")
                await self.telegram.send(
                    f"⚠️ *Could not enter captcha into field.* "
                    f"(Attempt {attempt}/{max_attempts})"
                )
                continue

            await asyncio.sleep(0.3)

            # Click Aktivieren to submit
            if not await self.click_aktivieren():
                print("[CAPTCHA] Could not locate the Aktivieren button.")
                await self.telegram.send(
                    f"⚠️ *Could not click Aktivieren.* "
                    f"(Attempt {attempt}/{max_attempts})"
                )
                continue

            # Check whether the CAPTCHA was accepted
            if not await self.is_captcha_error():
                print("[CAPTCHA] ✅ Captcha accepted — submission in progress.")
                return True

            # Wrong code — notify and loop
            print(f"[CAPTCHA] ❌ Wrong captcha code on attempt {attempt}.")
            remaining = max_attempts - attempt
            if remaining > 0:
                await self.telegram.send(
                    f"❌ *Wrong captcha code.* ({attempt}/{max_attempts} used)\n"
                    f"Sending a new image... "
                    f"{remaining} attempt{'s' if remaining != 1 else ''} remaining."
                )
            # Loop continues — reload + retry

        await self.telegram.send(
            f"❌ *Captcha failed after {max_attempts} attempts.*\n"
            "Booking aborted. Will retry on the next scheduled cycle."
        )
        return False
