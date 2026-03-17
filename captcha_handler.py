"""
Captcha Handler
───────────────
When the booking form presents a captcha:
  1. Take a focused screenshot of the captcha element
  2. [PRIMARY]  Try Gemini Vision API to auto-read the code
  3. [FALLBACK] If AI unavailable/wrong, send to Telegram and wait for a
                human reply (up to 3 minutes)
  4. Return the solution text (or None on timeout)

Retry flow (solve_with_retry):
  - On each attempt, tries Gemini first, then Telegram as fall-back
  - Clicks the Aktivieren button in the dialog
  - Detects "nicht korrekt" error and retries with a freshly reloaded image
  - Returns True when captcha accepted, False after max attempts or timeout
"""

import re
import os
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

        # ── [PRIMARY] Attempt AI-powered solving via Gemini ─────────────────
        ai_solution = await self._solve_with_gemini(screenshot_bytes)
        if ai_solution:
            print(f"[CAPTCHA] 🤖 Gemini auto-solved captcha: '{ai_solution}'")
            await self.telegram.send(
                f"🤖 *AI attempting captcha automatically:* `{ai_solution}`"
            )
            return ai_solution

        # ── [FALLBACK] Send to Telegram for manual solving ───────────────────
        print("[CAPTCHA] 🔄 Gemini unavailable/failed — requesting manual reply via Telegram.")
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

    async def _solve_with_gemini(self, screenshot_bytes: bytes) -> Optional[str]:
        """
        Attempt to read the CAPTCHA text using the Gemini Vision API.

        Reads GEMINI_API_KEY from the environment; returns None immediately if
        the key is absent or if the API call fails for any reason, so the caller
        can fall back to the manual Telegram flow without interruption.
        """
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print("[CAPTCHA] GEMINI_API_KEY not set — skipping AI CAPTCHA solve.")
            return None

        try:
            from google import genai          # lazy import — not needed everywhere
            from google.genai import types

            client = genai.Client(api_key=api_key)

            prompt = (
                "This image shows a CAPTCHA verification code used on a website. "
                "Your task is to read the exact characters displayed in the image. "
                "Reply with ONLY the CAPTCHA text (letters and/or digits). "
                "Do NOT include spaces, punctuation, explanations, or any other text. "
                "If the image is unclear or you are unsure, reply with an empty string."
            )

            response = await client.aio.models.generate_content(
                model="gemini-2.0-flash",
                contents=[
                    types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png"),
                    prompt,
                ],
            )

            raw = response.text.strip()
            # Sanity-check: CAPTCHA codes are typically 4–8 alphanumeric characters
            if raw and re.match(r'^[A-Za-z0-9]{3,10}$', raw):
                return raw

            print(f"[CAPTCHA] Gemini response failed sanity check: '{raw}'")
            return None

        except Exception as exc:
            print(f"[CAPTCHA] Gemini API error: {exc}")
            return None

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
                    await self._wait_for_loading()  # Instead of fixed sleep
                    return True
            except Exception:
                continue
        print("[CAPTCHA] Could not find Aktivieren button in dialog.")
        return False

    async def _wait_for_loading(self, timeout_seconds: int = 30) -> None:
        """Poll until the 'wird geladen' spinner is gone (matches BookingModule logic)."""
        IS_LOADING_JS = """
        () => {
          const walker = document.createTreeWalker(
            document.body, NodeFilter.SHOW_TEXT
          );
          let node;
          while ((node = walker.nextNode())) {
            if (node.textContent.trim().toLowerCase() === 'wird geladen') {
              const el = node.parentElement;
              if (el) {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                if (r.width > 0 && r.height > 0
                    && s.display !== 'none'
                    && s.visibility !== 'hidden'
                    && s.opacity !== '0') {
                  return true;
                }
              }
            }
          }
          const sels = [
            '[class*="spinner"]:not(dialog)',
            '[class*="loading-overlay"]',
            '[class*="page-loading"]',
          ];
          for (const sel of sels) {
            for (const el of document.querySelectorAll(sel)) {
              const s = window.getComputedStyle(el);
              if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0')
                continue;
              const r = el.getBoundingClientRect();
              if (r.width > 0 && r.height > 0) return true;
            }
          }
          return false;
        }
        """
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        poll_no  = 0
        while asyncio.get_event_loop().time() < deadline:
            try:
                still_loading = await self.page.evaluate(IS_LOADING_JS)
            except Exception:
                break
            if not still_loading:
                if poll_no > 0:
                    print(f"[CAPTCHA] ✅ Loading finished after ~{poll_no * 0.5:.1f}s.")
                break
            if poll_no == 0:
                print("[CAPTCHA] ⏳ Waiting for loading spinner to finish...")
            poll_no += 1
            await asyncio.sleep(0.5)
        else:
            print(f"[CAPTCHA] ⚠️ Loading still present after {timeout_seconds}s — proceeding anyway.")
        try:
            await self.page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass

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
