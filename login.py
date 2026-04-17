"""Sim24 login flow with storage-state reuse and CAPTCHA fallback."""

import os
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from typing import Tuple, Optional

try:
    from playwright_stealth import stealth_async
except Exception:
    async def stealth_async(page: Page) -> None:
        return None

LOGIN_URL   = "https://service.sim24.de/"
SUCCESS_URL = "https://service.sim24.de/mytariff"
DATA_URL    = "https://service.sim24.de/mytariff/invoice/showGprsDataUsage"
LOGIN_FORM_SELECTOR = "#UserLoginType_alias"
DASHBOARD_READY_SELECTORS = [
        "a[href*='/logout']",
        "a[href*='/mytariff']",
        "body",
]
STORAGE_STATE_PATH = Path(__file__).resolve().with_name("storage_state.json")
CAPTCHA_MAX_ATTEMPTS = 3

# Max login attempts (in case captcha is wrong or session quirk)
MAX_LOGIN_ATTEMPTS = 2


class Sim24Login:
    def __init__(self, username: str, password: str, telegram=None):
        self.username = username
        self.password = password
        self.telegram = telegram
        self._playwright = None  # stored so the caller can stop it after browser.close()

    async def login(self) -> Tuple[Optional[object], Optional[Page]]:
        """
        Launches Playwright, logs in, and returns (browser, page) ready for use.
        Returns (None, None) on failure.
        Caller is responsible for calling browser.close() when done.
        """
        from captcha_handler import CaptchaHandler

        playwright = await async_playwright().start()
        self._playwright = playwright

        # ── Browser selection ─────────────────────────────────────────────
        # USE_EDGE=true in .env → uses your installed Microsoft Edge
        # USE_EDGE not set    → uses Playwright's built-in Chromium (default)
        # Edge is recommended locally; Chromium is used on GitHub Actions
        # (Edge is not available on GitHub Actions runners)
        use_edge = os.environ.get("USE_EDGE", "false").lower() == "true"

        if use_edge:
            print("[LOGIN] Using Microsoft Edge")
            browser = await playwright.chromium.launch(
                channel="msedge",
                headless=True,
            )
        else:
            print("[LOGIN] Using Playwright Chromium")
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ]
            )

        context = await self._create_context(browser, use_storage_state=STORAGE_STATE_PATH.exists())
        page = await self._new_stealth_page(context)
        captcha = CaptchaHandler(page, self.telegram)

        session_reused = False

        if STORAGE_STATE_PATH.exists():
            print(f"[LOGIN] Found stored session: {STORAGE_STATE_PATH}")
            session_reused = await self._load_existing_session(page)
            if not session_reused:
                print("[LOGIN] Stored session is stale; falling back to credential login.")
                await context.close()
                context = await self._create_context(browser, use_storage_state=False)
                page = await self._new_stealth_page(context)
                captcha = CaptchaHandler(page, self.telegram)

        if not session_reused:
            for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
                print(f"[LOGIN] Attempt {attempt}/{MAX_LOGIN_ATTEMPTS}")

                try:
                    print(f"[LOGIN] Navigating to {LOGIN_URL}")
                    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
                    await page.wait_for_selector(LOGIN_FORM_SELECTOR, timeout=10_000)

                    if await captcha.is_captcha_present():
                        print("[LOGIN] Captcha detected on page load (Flow A).")
                        solved = await self._handle_login_captcha(captcha)
                        if not solved:
                            await browser.close()
                            return None, None

                    await page.fill(LOGIN_FORM_SELECTOR, self.username)
                    await page.fill("#UserLoginType_password", self.password)

                    await asyncio.sleep(0.8)

                    submit_clicked = await self._click_submit(page)
                    if not submit_clicked:
                        print("[LOGIN] No submit button matched any selector — trying JS submitForm()")
                        try:
                            await page.evaluate("submitForm('loginAction')")
                        except Exception:
                            print("[LOGIN] JS submitForm failed — pressing Enter on password field")
                            await page.press("#UserLoginType_password", "Enter")

                    await asyncio.sleep(2)

                    if await captcha.is_captcha_present():
                        print("[LOGIN] Captcha detected after submit (Flow B).")
                        solved = await self._handle_login_captcha(captcha)
                        if not solved:
                            await browser.close()
                            return None, None

                        print("[LOGIN] Re-submitting form after captcha solve...")
                        submit_clicked = await self._click_submit(page)
                        if not submit_clicked:
                            await page.evaluate("submitForm('loginAction')")
                        await asyncio.sleep(2)

                    current_url = page.url

                    if SUCCESS_URL in current_url:
                        print(f"[LOGIN] ✅ Login successful. URL: {current_url}")
                        await context.storage_state(path=str(STORAGE_STATE_PATH))
                        print(f"[LOGIN] Saved session state to {STORAGE_STATE_PATH}")
                        break

                    if "login" in current_url.lower():
                        error_text = await self._get_login_error(page)
                        if error_text:
                            print(f"[LOGIN] Login error on page: {error_text}")

                        if attempt < MAX_LOGIN_ATTEMPTS:
                            print(f"[LOGIN] Retrying... ({attempt}/{MAX_LOGIN_ATTEMPTS})")
                            await asyncio.sleep(2)
                            continue

                        print("[LOGIN] All attempts failed.")
                        await browser.close()
                        return None, None

                except Exception as e:
                    print(f"[LOGIN] Exception on attempt {attempt}: {e}")
                    if attempt >= MAX_LOGIN_ATTEMPTS:
                        await browser.close()
                        return None, None
                    await asyncio.sleep(2)

        try:
            print("[LOGIN] Navigating to data usage page...")
            await page.goto(DATA_URL, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_selector(".e-data_usage_meter", timeout=10_000)
            print("[LOGIN] Data usage page loaded successfully.")
            return browser, page

        except Exception as e:
            print(f"[LOGIN] Failed to load data usage page: {e}")
            await browser.close()
            return None, None

    async def _create_context(self, browser: Browser, use_storage_state: bool) -> BrowserContext:
        context_options = {
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "viewport": {"width": 1280, "height": 900},
            "locale": "de-DE",
        }
        if use_storage_state:
            context_options["storage_state"] = str(STORAGE_STATE_PATH)
        return await browser.new_context(**context_options)

    async def _new_stealth_page(self, context: BrowserContext) -> Page:
        page = await context.new_page()
        await stealth_async(page)
        return page

    async def _load_existing_session(self, page: Page) -> bool:
        print(f"[LOGIN] Trying stored session via {SUCCESS_URL}")
        await page.goto(SUCCESS_URL, wait_until="domcontentloaded", timeout=30_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass

        current_url = page.url.lower()
        if "login" in current_url:
            return False

        if SUCCESS_URL in page.url:
            for selector in DASHBOARD_READY_SELECTORS:
                try:
                    await page.wait_for_selector(selector, timeout=5_000)
                    print("[LOGIN] Stored session accepted by dashboard.")
                    return True
                except Exception:
                    continue

        return False

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _handle_login_captcha(self, captcha) -> bool:
        from captcha_handler import CaptchaAutomationError, CaptchaSolveError

        last_error = None

        for attempt in range(1, CAPTCHA_MAX_ATTEMPTS + 1):
            if attempt > 1:
                await captcha.reload_captcha_image()

            try:
                solution = await captcha.solve()
                if solution is None:
                    raise CaptchaAutomationError("Login captcha was not available for solving.")

                print(f"[LOGIN] Captcha solution entered: {solution}")
                return True
            except CaptchaAutomationError as exc:
                last_error = exc
                print(f"[LOGIN] Captcha attempt {attempt} failed: {exc}")

        raise CaptchaSolveError(
            f"Gemini failed to solve the login captcha after {CAPTCHA_MAX_ATTEMPTS} attempts."
        ) from last_error

    async def _click_submit(self, page: Page) -> bool:
        """Try various ways to click the login submit button."""
        selectors = [
            "a[onclick=\"submitForm('loginAction');\"]",
            "a.c-button[title='Login']",
            "a.submitOnEnter",
        ]
        for selector in selectors:
            try:
                btn = await page.query_selector(selector)
                if btn and await btn.is_visible():
                    await btn.click()
                    return True
            except Exception:
                continue
        return False

    async def _get_login_error(self, page: Page) -> Optional[str]:
        """Extract error message text from the login page if present."""
        error_selectors = [
            ".alert-danger",
            ".error-message",
            ".noticeBox-error",
            "[class*='error']",
        ]
        for selector in error_selectors:
            try:
                el = await page.query_selector(selector)
                if el:
                    text = await el.inner_text()
                    return text.strip()
            except Exception:
                continue
        return None
