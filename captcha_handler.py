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
            "return !!el && el.complete && el.naturalWidth > 0; }",
            selector,
            timeout=timeout_ms,
        )
    except Exception:
        pass  # Proceed anyway — the screenshot may still be usable


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


async def solve_captcha_with_gemini(page: Page, captcha_element_selector: str) -> str:
    """Capture a CAPTCHA image, solve it with Gemini, and fill the input field."""
    captcha_element = page.locator(captcha_element_selector).first
    await captcha_element.wait_for(state="visible", timeout=10_000)
    await _wait_for_image_load(page, captcha_element_selector)

    screenshot_bytes = await captcha_element.screenshot(type="png")
    image_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
    solution = await _extract_gemini_text(image_b64)

    entered = await _fill_captcha_input(page, solution)
    if not entered:
        raise CaptchaAutomationError("CAPTCHA input field was not found.")

    return solution


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

        solution = await solve_captcha_with_gemini(self.page, selector)
        print(f"[CAPTCHA] Gemini solved captcha as: {solution}")
        return solution

    async def _screenshot_captcha(self) -> Optional[bytes]:
        """Return raw PNG bytes of the CAPTCHA <img> element only."""
        selector = await self.get_captcha_selector()
        if selector is None:
            return None
        try:
            captcha_element = self.page.locator(selector).first
            await _wait_for_image_load(self.page, selector)
            return await captcha_element.screenshot(type="png")
        except Exception as exc:
            print(f"[CAPTCHA] Could not screenshot captcha element: {exc}")
            return None

    async def _request_manual_solution(self) -> Optional[str]:
        """Send the CAPTCHA image to Telegram and wait for the user to reply via the scheduler bot."""
        if self.telegram is None:
            print("[CAPTCHA] No Telegram notifier — cannot request manual solve.")
            return None

        if self.config_manager is None:
            print("[CAPTCHA] No config manager — cannot request manual solve.")
            return None

        image_bytes = await self._screenshot_captcha()
        if image_bytes is None:
            print("[CAPTCHA] Could not capture captcha image for manual solve.")
            return None

        # Mark captcha as pending in Gist so the scheduler bot forwards the reply.
        try:
            self.config_manager.set_captcha_pending(True)
        except Exception as exc:
            print(f"[CAPTCHA] Could not set captcha_pending in Gist: {exc}")
            return None

        # Send a text alert first — text messages trigger push notifications more
        # reliably than photos, ensuring the user sees the request immediately.
        await self.telegram.send(
            "\U0001f6a8 *ACTION REQUIRED — Manual CAPTCHA*\n"
            "Gemini could not solve the captcha.\n"
            "The captcha image follows. *Reply with the code within 5 minutes.*"
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
            print("[CAPTCHA] Failed to send captcha image to Telegram.")
            try:
                self.config_manager.clear_captcha_state()
            except Exception:
                pass
            return None

        print("[CAPTCHA] Captcha image sent to Telegram. Waiting for manual reply (up to 5 min)...")

        # Poll Gist every few seconds for up to 2 minutes.
        solution: Optional[str] = None
        timeout_seconds = MANUAL_CAPTCHA_TIMEOUT_SECONDS
        poll_interval = MANUAL_CAPTCHA_POLL_INTERVAL_SECONDS
        for _ in range(timeout_seconds // poll_interval):
            try:
                reply = self.config_manager.get_captcha_reply()
                if reply:
                    solution = re.sub(r"[^A-Za-z0-9]", "", reply).strip() or None
                    if solution:
                        print(f"[CAPTCHA] Manual solution received from Telegram: {solution}")
                        break
            except Exception as exc:
                print(f"[CAPTCHA] Error polling Gist for captcha reply: {exc}")
            await asyncio.sleep(poll_interval)

        try:
            self.config_manager.clear_captcha_state()
        except Exception:
            pass

        if not solution:
            print("[CAPTCHA] Timed out waiting for manual captcha reply.")
            await self.telegram.send(
                "\u23f3 *CAPTCHA manual input timed out.*\n"
                "No reply received within 5 minutes. The booking attempt has been abandoned."
            )

        return solution

    async def _enter_manual_solution(self, reason: str) -> None:
        """Request a manual CAPTCHA solve, then enter it into the form field."""
        print(reason)
        solution = await self._request_manual_solution()
        if solution is None:
            raise CaptchaAutomationError("No manual captcha solution received.")

        entered = await _fill_captcha_input(self.page, solution)
        if not entered:
            raise CaptchaAutomationError("CAPTCHA input field was not found.")

        print(f"[CAPTCHA] Manual solution entered: {solution}")

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

    async def solve_with_retry(self, max_attempts: int = 3) -> bool:
        last_error: Optional[Exception] = None
        use_manual_on_last = (
            self.telegram is not None and self.config_manager is not None
        )

        for attempt in range(1, max_attempts + 1):
            print(f"[CAPTCHA] Solve attempt {attempt}/{max_attempts}")

            if attempt > 1:
                await self.reload_captcha_image()

            is_last = attempt == max_attempts
            try:
                used_manual_solution = False
                if is_last and use_manual_on_last:
                    used_manual_solution = True
                    await self._enter_manual_solution(
                        "[CAPTCHA] Last attempt — requesting manual solve via Telegram."
                    )
                else:
                    solution: Optional[str] = None
                    try:
                        solution = await self.solve()
                    except Exception as exc:
                        if use_manual_on_last:
                            print(f"[CAPTCHA] Gemini solve failed: {exc}")
                            used_manual_solution = True
                            await self._enter_manual_solution(
                                "[CAPTCHA] Falling back to manual solve via Telegram."
                            )
                        else:
                            if isinstance(exc, CaptchaAutomationError):
                                raise
                            raise CaptchaAutomationError(
                                f"Gemini solve failed: {exc}"
                            ) from exc
                    if solution is None and not used_manual_solution:
                        raise CaptchaAutomationError("Captcha image was not available for solving.")

                if not await self.click_aktivieren():
                    raise CaptchaAutomationError("Aktivieren button was not clickable.")

                if not await self.is_captcha_error():
                    print("[CAPTCHA] Captcha accepted.")
                    return True

                last_error = CaptchaAutomationError("CAPTCHA answer was incorrect.")
                print(f"[CAPTCHA] Incorrect captcha answer on attempt {attempt}.")
            except CaptchaAutomationError as exc:
                last_error = exc
                print(f"[CAPTCHA] Attempt {attempt} failed: {exc}")

        raise CaptchaSolveError(
            f"Gemini failed to solve the captcha after {max_attempts} attempts."
        ) from last_error
