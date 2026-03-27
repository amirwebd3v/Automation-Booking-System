"""
Sim24 Login Module
──────────────────
Handles a fresh login every run cycle (no session persistence needed
because the site times out after 10 minutes anyway).

Login flow:
  1. Navigate to the login page
  2. Fill username + password
  3. Check for captcha BEFORE submitting (Flow A — captcha on page load)
  4. Submit the form
  5. Check for captcha AFTER submitting (Flow B — captcha appears post-submit)
  6. Verify login success by checking for post-login URL or element

Both Flow A and Flow B are handled via the same CaptchaHandler (Telegram photo → user reply).
The TelegramNotifier is passed in from main.py so it's shared across all modules.
"""

import os
import asyncio
from playwright.async_api import async_playwright, Browser, Page
from typing import Tuple, Optional

# CaptchaHandler is imported here — it needs the page and telegram notifier
# We import lazily inside the method to avoid circular imports at module load
LOGIN_URL   = "https://service.sim24.de/"
SUCCESS_URL = "https://service.sim24.de/mytariff"
DATA_URL    = "https://service.sim24.de/mytariff/invoice/showGprsDataUsage"

# Max login attempts (in case captcha is wrong or session quirk)
MAX_LOGIN_ATTEMPTS = 2


class Sim24Login:
    def __init__(self, username: str, password: str, telegram=None):
        self.username = username
        self.password = password
        self.telegram = telegram  # TelegramNotifier instance (optional but needed for captcha)

    async def login(self) -> Tuple[Optional[object], Optional[Page]]:
        """
        Launches Playwright, logs in, and returns (browser, page) ready for use.
        Returns (None, None) on failure.
        Caller is responsible for calling browser.close() when done.
        """
        # Import here to avoid circular dependency (CaptchaHandler also imports TelegramNotifier)
        from .captcha_handler import CaptchaHandler

        playwright = await async_playwright().start()

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

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="de-DE",
        )

        page = await context.new_page()
        captcha = CaptchaHandler(page, self.telegram)

        for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
            print(f"[LOGIN] Attempt {attempt}/{MAX_LOGIN_ATTEMPTS}")

            try:
                # ── Navigate to login page ────────────────────────────────────
                print(f"[LOGIN] Navigating to {LOGIN_URL}")
                await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_selector("#UserLoginType_alias", timeout=10_000)

                # ── Flow A: Check for captcha BEFORE filling the form ─────────
                # Some portals show captcha immediately on page load
                if await captcha.is_captcha_present():
                    print("[LOGIN] Captcha detected on page load (Flow A).")
                    solved = await self._handle_login_captcha(captcha, page)
                    if not solved:
                        await browser.close()
                        return None, None

                # ── Fill credentials ──────────────────────────────────────────
                await page.fill("#UserLoginType_alias",    self.username)
                await page.fill("#UserLoginType_password", self.password)

                # Small human-like delay
                await asyncio.sleep(0.8)

                # ── Submit form ───────────────────────────────────────────────
                # Try multiple selectors first; fall back to direct JS call
                submit_clicked = await self._click_submit(page)
                if not submit_clicked:
                    print("[LOGIN] No submit button matched any selector — trying JS submitForm()")
                    try:
                        await page.evaluate("submitForm('loginAction')")
                    except Exception:
                        # Last resort: press Enter on the password field
                        print("[LOGIN] JS submitForm failed — pressing Enter on password field")
                        await page.press("#UserLoginType_password", "Enter")

                # ── Wait briefly then check what happened ─────────────────────
                await asyncio.sleep(2)

                # ── Flow B: Check for captcha AFTER submit ────────────────────
                # Some portals only show captcha after a login attempt
                if await captcha.is_captcha_present():
                    print("[LOGIN] Captcha detected after submit (Flow B).")
                    solved = await self._handle_login_captcha(captcha, page)
                    if not solved:
                        await browser.close()
                        return None, None

                    # Re-submit after solving the captcha
                    print("[LOGIN] Re-submitting form after captcha solve...")
                    submit_clicked = await self._click_submit(page)
                    if not submit_clicked:
                        # Last resort: submit the form directly
                        await page.evaluate("submitForm('loginAction')")
                    await asyncio.sleep(2)

                # ── Check login result ────────────────────────────────────────
                current_url = page.url

                if SUCCESS_URL in current_url:
                    print(f"[LOGIN] ✅ Login successful. URL: {current_url}")
                    break  # Exit the retry loop

                # Still on login page — check why
                if "login" in current_url.lower():
                    error_text = await self._get_login_error(page)
                    if error_text:
                        print(f"[LOGIN] Login error on page: {error_text}")

                    if attempt < MAX_LOGIN_ATTEMPTS:
                        print(f"[LOGIN] Retrying... ({attempt}/{MAX_LOGIN_ATTEMPTS})")
                        await asyncio.sleep(2)
                        continue
                    else:
                        print("[LOGIN] All attempts failed.")
                        if self.telegram:
                            await self.telegram.send(
                                "❌ *Login failed after all attempts.*\n"
                                f"Last error: `{error_text or 'Unknown error'}`\n"
                                "Please check your credentials."
                            )
                        await browser.close()
                        return None, None

            except Exception as e:
                print(f"[LOGIN] Exception on attempt {attempt}: {e}")
                if attempt >= MAX_LOGIN_ATTEMPTS:
                    await browser.close()
                    return None, None
                await asyncio.sleep(2)

        # ── Navigate to data usage page ───────────────────────────────────────
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

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _handle_login_captcha(self, captcha, page) -> bool:
        """
        Sends captcha image to Telegram, waits for user reply,
        enters the solution into the captcha field.
        Returns True if solution was entered, False on timeout or failure.
        """
        if self.telegram:
            await self.telegram.send(
                "🔐 *Captcha on Login Page*\n"
                "The login form requires a captcha.\n"
                "Sending image now..."
            )

        solution = await captcha.solve()
        if solution is None:
            # solve() already sent the timeout notification
            return False

        entered = await captcha.enter_solution(solution)
        if not entered:
            if self.telegram:
                await self.telegram.send(
                    "❌ *Could not enter captcha solution on login page.*\n"
                    "The captcha input field was not found."
                )
            return False

        print(f"[LOGIN] Captcha solution entered: {solution}")
        return True

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
