"""Autonomous CAPTCHA handling backed by Gemini 1.5 Flash."""

import asyncio
import base64
import os
import re
from typing import Optional

from playwright.async_api import Page

try:
    import google.generativeai as genai
except ImportError:
    genai = None


CAPTCHA_PROMPT = (
    "This is a CAPTCHA image containing distorted or wavy text. "
    "Read every character in the image extremely carefully, one by one, "
    "from left to right. The text is typically 5-6 alphanumeric characters "
    "and may be lowercase letters, uppercase letters, or digits. "
    "Characters are often warped, tilted, or overlapping — examine each one "
    "individually. Return ONLY the exact characters you see with no spaces, "
    "no punctuation, and no explanation. Do not guess; transcribe only what "
    "is clearly visible in the image."
)

CAPTCHA_SELECTORS = [
    "img[src*='captcha']",
    "img[alt*='captcha']",
    "img[alt*='Captcha']",
    ".captcha img",
    "#captcha img",
    "img[src*='securimage']",
    "img[src*='code']",
]

CAPTCHA_INPUT_SELECTORS = [
    ".captchaInput input",
    "input[name*='captcha']",
    "input[id*='captcha']",
    "input[name*='code']",
    "#captcha_code",
]

CAPTCHA_RELOAD_SELECTORS = [
    "a.captcha_reload",
    "a[href^='javascript:reload_captcha']",
    ".reload a",
]

CAPTCHA_ERROR_TEXTS = [
    "der eingegebene code ist nicht korrekt",
    "code ist nicht korrekt",
    "captcha ist nicht korrekt",
    "ungültiger code",
    "ungültiger captcha",
]

MODAL_SELECTOR = "dialog#c-overlay"
SPINNER_SELECTORS = [
    ".loading-overlay",
    ".page-loading",
    ".spinner",
    "[class*='loading-overlay']",
    "[class*='page-loading']",
    "[class*='spinner']",
]

MANUAL_CAPTCHA_TIMEOUT_SECONDS = 300
MANUAL_CAPTCHA_POLL_INTERVAL_SECONDS = 3


class CaptchaAutomationError(RuntimeError):
    """Base class for autonomous CAPTCHA errors."""


class CaptchaConfigurationError(CaptchaAutomationError):
    """Raised when required Gemini configuration is missing."""


class CaptchaSolveError(CaptchaAutomationError):
    """Raised when Gemini cannot solve the CAPTCHA within the retry budget."""


_CONFIGURED_GEMINI_KEY: Optional[str] = None
_RESOLVED_GEMINI_MODEL: Optional[str] = None


def _configure_gemini() -> None:
    global _CONFIGURED_GEMINI_KEY

    if genai is None:
        raise CaptchaConfigurationError("google-generativeai is not installed.")

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise CaptchaConfigurationError("GEMINI_API_KEY is not configured.")

    if _CONFIGURED_GEMINI_KEY != api_key:
        genai.configure(api_key=api_key)
        _CONFIGURED_GEMINI_KEY = api_key


def _resolve_gemini_model() -> str:
    global _RESOLVED_GEMINI_MODEL

    if _RESOLVED_GEMINI_MODEL is not None:
        return _RESOLVED_GEMINI_MODEL

    _configure_gemini()

    configured_model = os.environ.get("GEMINI_MODEL", "").strip()
    preferred_models = [
        configured_model,
        "gemini-1.5-flash",
        "gemini-flash-latest",
        "gemini-2.0-flash",
        "gemini-2.5-flash",
    ]

    available_models = {
        model.name for model in genai.list_models()
        if "generateContent" in getattr(model, "supported_generation_methods", [])
    }

    for model_name in preferred_models:
        if not model_name:
            continue
        normalized_name = model_name if model_name.startswith("models/") else f"models/{model_name}"
        if normalized_name in available_models:
            _RESOLVED_GEMINI_MODEL = normalized_name.removeprefix("models/")
            print(f"[CAPTCHA] Using Gemini model: {_RESOLVED_GEMINI_MODEL}")
            return _RESOLVED_GEMINI_MODEL

    raise CaptchaConfigurationError(
        "No supported Gemini Flash model is available for this API key."
    )


async def _wait_for_image_load(page: Page, selector: str, timeout_ms: int = 10_000) -> None:
    """Wait until the <img> matched by *selector* has fully loaded its pixel data."""
    try:
        await page.wait_for_function(
            "(sel) => { const el = document.querySelector(sel); "
            "return !!el && el.complete && el.naturalWidth > 0 && el.naturalHeight > 0; }",
            selector,
            timeout=timeout_ms,
        )
        # Brief pause so the browser has time to paint the decoded pixels before
        # we take a screenshot.
        await asyncio.sleep(0.15)
    except Exception:
        pass  # Proceed anyway — direct fetch will be attempted first


async def _fetch_captcha_image_directly(page: Page, selector: str) -> Optional[bytes]:
    """Fetch captcha image bytes via the browser's HTTP session rather than a DOM screenshot.

    Using ``page.request.get()`` shares the browser context's cookies so the
    server returns exactly the image it will validate against.  This avoids the
    blank/white image artefact that can occur when Playwright screenshots a
    ``<img>`` whose pixels haven't been painted yet.
    """
    try:
        src: Optional[str] = await page.locator(selector).first.get_attribute("src")
        if not src:
            return None
        # Resolve relative URLs against the page's base URI.
        abs_url: str = await page.evaluate("(s) => new URL(s, document.baseURI).href", src)
        response = await page.request.get(abs_url)
        if response.ok:
            body = await response.body()
            if body:
                return body
    except Exception as exc:
        print(f"[CAPTCHA] Direct captcha image fetch failed: {exc}")
    return None


async def _extract_gemini_text(image_b64: str) -> str:
    model = genai.GenerativeModel(_resolve_gemini_model())
    response = await asyncio.to_thread(
        model.generate_content,
        [
            CAPTCHA_PROMPT,
            {
                "mime_type": "image/png",
                "data": image_b64,
            },
        ],
        generation_config={"temperature": 0},
    )

    response_text = getattr(response, "text", "") or ""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", response_text).strip()
    if not cleaned:
        raise CaptchaAutomationError("Gemini returned an empty CAPTCHA response.")
    return cleaned


async def _find_first_visible_selector(page: Page, selectors: list[str]) -> Optional[str]:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count() and await locator.is_visible():
                return selector
        except Exception:
            continue
    return None


async def _fill_captcha_input(page: Page, solution: str) -> bool:
    input_selector = await _find_first_visible_selector(page, CAPTCHA_INPUT_SELECTORS)
    if input_selector is None:
        return False

    field = page.locator(input_selector).first
    await field.fill(solution)
    return True


async def solve_captcha_with_gemini(page: Page, captcha_element_selector: str) -> tuple[str, bytes]:
    """Capture a CAPTCHA image, solve it with Gemini, and fill the input field.

    Returns a ``(solution, screenshot_bytes)`` tuple so callers can forward the
    exact image that Gemini saw without taking a second, potentially stale screenshot.

    Image acquisition strategy (in order):
    1. Direct HTTP fetch via ``page.request`` — shares the browser session cookies,
       guarantees the server-side image, and avoids blank/white rendering artefacts.
    2. Element screenshot fallback — used only when the HTTP fetch fails.
    """
    captcha_element = page.locator(captcha_element_selector).first
    await captcha_element.wait_for(state="visible", timeout=10_000)

    # Preferred: fetch the image bytes directly from the server.
    image_bytes = await _fetch_captcha_image_directly(page, captcha_element_selector)

    if image_bytes is None:
        # Fallback: wait for browser render and screenshot the element.
        await _wait_for_image_load(page, captcha_element_selector)
        image_bytes = await captcha_element.screenshot(type="png")

    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    solution = await _extract_gemini_text(image_b64)

    entered = await _fill_captcha_input(page, solution)
    if not entered:
        raise CaptchaAutomationError("CAPTCHA input field was not found.")

    return solution, image_bytes


class CaptchaHandler:
    def __init__(self, page: Page, telegram=None, config_manager=None):
        self.page = page
        self.telegram = telegram
        self.config_manager = config_manager

    async def get_captcha_selector(self) -> Optional[str]:
        return await _find_first_visible_selector(self.page, CAPTCHA_SELECTORS)

    async def is_captcha_present(self) -> bool:
        selector = await self.get_captcha_selector()
        if selector:
            print(f"[CAPTCHA] Found captcha via selector: {selector}")
            return True
        return False

    async def solve(self) -> Optional[str]:
        selector = await self.get_captcha_selector()
        if selector is None:
            print("[CAPTCHA] Could not find a visible captcha image.")
            return None

        solution, _ = await solve_captcha_with_gemini(self.page, selector)
        print(f"[CAPTCHA] Gemini solved captcha as: {solution}")
        return solution

    async def _solve_with_screenshot(self) -> tuple[Optional[str], Optional[bytes]]:
        """Like solve(), but also returns the screenshot bytes Gemini actually saw."""
        selector = await self.get_captcha_selector()
        if selector is None:
            print("[CAPTCHA] Could not find a visible captcha image.")
            return None, None

        solution, screenshot_bytes = await solve_captcha_with_gemini(self.page, selector)
        print(f"[CAPTCHA] Gemini solved captcha as: {solution}")
        return solution, screenshot_bytes

    async def _screenshot_captcha(self) -> Optional[bytes]:
        """Return captcha image bytes — direct HTTP fetch first, element screenshot as fallback."""
        selector = await self.get_captcha_selector()
        if selector is None:
            return None
        # Preferred: fetch directly from the server to avoid blank rendering artefacts.
        image_bytes = await _fetch_captcha_image_directly(self.page, selector)
        if image_bytes:
            return image_bytes
        # Fallback: element screenshot.
        try:
            captcha_element = self.page.locator(selector).first
            await _wait_for_image_load(self.page, selector)
            return await captcha_element.screenshot(type="png")
        except Exception as exc:
            print(f"[CAPTCHA] Could not screenshot captcha element: {exc}")
            return None

    async def _get_captcha_src(self) -> Optional[str]:
        """Return the current ``src`` attribute of the captcha <img> element."""
        selector = await self.get_captcha_selector()
        if selector is None:
            return None
        try:
            return await self.page.locator(selector).first.get_attribute("src")
        except Exception:
            return None

    async def _solve_manually_until_accepted(self) -> None:
        """Infinite manual-solve loop: send captcha image to Telegram, submit answer, repeat.

        The loop only exits when:
        - The submitted code is accepted by the site, OR
        - The user does not reply within ``MANUAL_CAPTCHA_TIMEOUT_SECONDS`` (raises error).

        Staleness is handled at two points:
        1. *After receiving the reply* — the captcha ``src`` is compared with the one
           that was screenshotted before sending; if they differ the image is re-sent.
        2. *After filling the input* — the src is checked once more before clicking
           Aktivieren; if it changed the loop continues immediately with a fresh image.
        """
        if self.telegram is None:
            raise CaptchaAutomationError("No Telegram notifier — cannot request manual solve.")

        round_number = 0
        while True:
            round_number += 1
            print(f"[CAPTCHA] Manual solve round {round_number}")

            # --- 1. Capture the captcha image and record its src ---
            src_before = await self._get_captcha_src()
            image_bytes = await self._screenshot_captcha()
            if image_bytes is None:
                raise CaptchaAutomationError("Could not capture captcha image for manual solve.")

            # --- 2. Send image to Telegram and wait for user's reply ---
            if round_number == 1:
                await self.telegram.send(
                    "\U0001f6a8 *ACTION REQUIRED — Manual CAPTCHA*\n"
                    "Gemini could not solve the captcha.\n"
                    "The captcha image follows. *Reply with the code within 5 minutes.*"
                )
            else:
                await self.telegram.send(
                    "\u26a0\ufe0f *New captcha image — please try again.*\n"
                    "Reply with the code shown in the image below."
                )

            sent = await self.telegram.send_photo(
                image_bytes,
                caption=(
                    "\U0001f510 *Manual CAPTCHA required*\n"
                    "Please *reply to this message* with the code shown above.\n"
                    "_You have 5 minutes._"
                ),
            )
            if not sent:
                raise CaptchaAutomationError("Failed to send captcha image to Telegram.")

            print("[CAPTCHA] Captcha image sent to Telegram. Waiting for manual reply (up to 5 min)...")

            raw_reply = await self.telegram.wait_for_reply(
                timeout_seconds=MANUAL_CAPTCHA_TIMEOUT_SECONDS,
                poll_interval=MANUAL_CAPTCHA_POLL_INTERVAL_SECONDS,
            )

            if not raw_reply:
                print("[CAPTCHA] Timed out waiting for manual captcha reply.")
                await self.telegram.send(
                    "\u23f3 *CAPTCHA manual input timed out.*\n"
                    "No reply received within 5 minutes. The booking attempt has been abandoned."
                )
                raise CaptchaAutomationError("Manual captcha timed out — no reply from user.")

            # --- 3. Staleness check #1: did the captcha change while we waited? ---
            src_after_reply = await self._get_captcha_src()
            if src_before and src_after_reply and src_before != src_after_reply:
                print("[CAPTCHA] Captcha refreshed during Telegram wait — sending new image.")
                await self.reload_captcha_image()
                continue  # Restart loop with the new captcha

            solution = re.sub(r"[^A-Za-z0-9]", "", raw_reply).strip()
            if not solution:
                print("[CAPTCHA] Empty reply received — asking again.")
                continue

            print(f"[CAPTCHA] Manual solution received from Telegram: {solution}")

            # --- 4. Fill input field ---
            entered = await _fill_captcha_input(self.page, solution)
            if not entered:
                raise CaptchaAutomationError("CAPTCHA input field was not found.")
            print(f"[CAPTCHA] Manual solution entered: {solution}")

            # --- 5. Staleness check #2: did the captcha change after fill()? ---
            src_after_fill = await self._get_captcha_src()
            if src_before and src_after_fill and src_before != src_after_fill:
                print("[CAPTCHA] Captcha refreshed after fill — sending new image.")
                await self.reload_captcha_image()
                continue

            # --- 6. Submit ---
            if not await self.click_aktivieren():
                raise CaptchaAutomationError("Aktivieren button was not clickable.")

            if not await self.is_captcha_error():
                print("[CAPTCHA] Manual captcha accepted.")
                return  # Done!

            print("[CAPTCHA] Manual captcha rejected — reloading and trying again.")
            await self.reload_captcha_image()
            # Loop continues

    async def enter_solution(self, solution: str) -> bool:
        entered = await _fill_captcha_input(self.page, solution)
        if entered:
            print("[CAPTCHA] Entered solution into the captcha input field.")
        else:
            print("[CAPTCHA] Could not find captcha input field.")
        return entered

    async def is_captcha_error(self) -> bool:
        try:
            content = (await self.page.content()).lower()
            return any(err in content for err in CAPTCHA_ERROR_TEXTS)
        except Exception:
            return False

    async def reload_captcha_image(self) -> bool:
        current_selector = await self.get_captcha_selector()
        current_src = None
        if current_selector is not None:
            try:
                current_src = await self.page.locator(current_selector).first.get_attribute("src")
            except Exception:
                current_src = None

        for selector in CAPTCHA_RELOAD_SELECTORS:
            locator = self.page.locator(selector).first
            try:
                if not await locator.count() or not await locator.is_visible():
                    continue

                await locator.click()
                if current_selector is not None and current_src:
                    try:
                        await self.page.wait_for_function(
                            "([sel, src]) => {"
                            "  const el = document.querySelector(sel);"
                            "  return !!el && el.getAttribute('src') !== src;"
                            "}",
                            [current_selector, current_src],
                            timeout=10_000,
                        )
                    except Exception:
                        pass

                    # Wait for the new image to fully render before we screenshot it
                    if current_selector:
                        await _wait_for_image_load(self.page, current_selector)

                await self.wait_for_loading(timeout_seconds=10)
                print(f"[CAPTCHA] Captcha image reloaded via: {selector}")
                return True
            except Exception:
                continue

        print("[CAPTCHA] Could not reload captcha image.")
        return False

    async def _notify_gemini_answer(self, solution: str, screenshot_bytes: Optional[bytes] = None) -> None:
        """Send the captcha image Gemini saw and its answer to Telegram before submitting.

        Pass *screenshot_bytes* captured at solve-time so this method never
        re-screenshots — by the time we notify, the captcha image on the page
        may already have been refreshed by the site's JS.
        """
        if self.telegram is None:
            return
        if screenshot_bytes is None:
            await self.telegram.send(f"\U0001f916 *Gemini CAPTCHA answer:* `{solution}`")
            return
        await self.telegram.send_photo(
            screenshot_bytes,
            caption=(
                f"\U0001f916 *Gemini answered:* `{solution}`\n"
                "_Check if this matches the image above._"
            ),
        )

    async def click_aktivieren(self) -> bool:
        modal = self.page.locator(MODAL_SELECTOR).first
        try:
            await modal.wait_for(state="visible", timeout=10_000)
        except Exception:
            print("[CAPTCHA] Modal container did not become visible.")
            return False

        candidates = [
            modal.get_by_role("link", name="Aktivieren").first,
            modal.locator("a[title='Aktivieren']").first,
            modal.locator("a:has-text('Aktivieren')").first,
        ]

        for locator in candidates:
            try:
                if not await locator.count() or not await locator.is_visible():
                    continue

                await locator.click()
                print("[CAPTCHA] Clicked Aktivieren in modal.")
                await self.wait_for_loading()
                return True
            except Exception:
                continue

        print("[CAPTCHA] Could not find Aktivieren button in modal.")
        return False

    async def wait_for_loading(self, timeout_seconds: int = 30) -> None:
        timeout_ms = timeout_seconds * 1000
        for selector in SPINNER_SELECTORS:
            locator = self.page.locator(selector).first
            try:
                if await locator.count() and await locator.is_visible():
                    await self.page.wait_for_selector(selector, state="hidden", timeout=timeout_ms)
            except Exception:
                continue

        try:
            loading_text = self.page.get_by_text("Wird geladen")
            if await loading_text.first.count() and await loading_text.first.is_visible():
                await loading_text.first.wait_for(state="hidden", timeout=timeout_ms)
        except Exception:
            pass

        try:
            await self.page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass

    async def solve_with_retry(self, max_gemini_attempts: int = 2) -> bool:
        """Solve the captcha with an unlimited overall retry budget.

        Phase 1 — Gemini auto-solve (up to *max_gemini_attempts* attempts).
            Each Gemini attempt reloads the captcha first (except the very first),
            lets Gemini solve it, notifies Telegram with the image + answer, then
            submits.  On success the function returns immediately.
            If Gemini fails for *any* reason (wrong answer, exception, config error,
            image unavailable …) the loop ends early and Phase 2 takes over.

        Phase 2 — Manual solve via Telegram (unlimited rounds).
            Always used as the fallback whenever Gemini doesn't succeed.
            Loops indefinitely: sends the current captcha image to Telegram, waits
            for the user's reply, verifies the captcha hasn't refreshed at two
            check-points (after reply and after fill), submits, and repeats if
            rejected.  The only exit is success or a 5-minute user timeout.
        """
        gemini_succeeded = False

        if self.telegram is None and max_gemini_attempts == 0:
            raise CaptchaSolveError("No Gemini attempts and no Telegram configured.")

        # ── Phase 1: Gemini ──────────────────────────────────────────────────
        for attempt in range(1, max_gemini_attempts + 1):
            print(f"[CAPTCHA] Gemini attempt {attempt}/{max_gemini_attempts}")

            if attempt > 1:
                await self.reload_captcha_image()

            try:
                solution, captcha_screenshot = await self._solve_with_screenshot()
                if solution is None:
                    print(f"[CAPTCHA] Gemini attempt {attempt}: captcha image unavailable — falling back to manual.")
                    break

                await self._notify_gemini_answer(solution, captcha_screenshot)

                if not await self.click_aktivieren():
                    print(f"[CAPTCHA] Gemini attempt {attempt}: Aktivieren not clickable — falling back to manual.")
                    break

                if not await self.is_captcha_error():
                    print("[CAPTCHA] Captcha accepted.")
                    gemini_succeeded = True
                    return True

                print(f"[CAPTCHA] Incorrect captcha answer on Gemini attempt {attempt}.")

            except Exception as exc:
                print(f"[CAPTCHA] Gemini attempt {attempt} failed ({type(exc).__name__}: {exc}) — falling back to manual.")
                break

        # ── Phase 2: Manual (unlimited) ───────────────────────────────────────
        if gemini_succeeded:
            return True  # already returned above, but keeps the guard explicit

        if self.telegram is None:
            raise CaptchaSolveError(
                "Gemini could not solve the captcha and no Telegram is configured for manual fallback."
            )

        print("[CAPTCHA] Switching to unlimited manual solve via Telegram.")
        await self.reload_captcha_image()
        await self._solve_manually_until_accepted()
        return True


