"""
Captcha Handler
───────────────
When the booking form presents a captcha:
  1. Take a focused screenshot of the captcha element
  2. Send it to Telegram with an urgent prompt
  3. Wait up to 3 minutes for the user to reply with the solution
  4. Return the solution text (or None on timeout)
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
