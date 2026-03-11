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
  3. Handle cookie consent (may block click)
  4. Click the Aktivieren button in the confirmation modal
  5. Verify success
"""

import asyncio
from playwright.async_api import Page
from telegram_notify import TelegramNotifier
from captcha_handler import CaptchaHandler


BOOK_BUTTON_ID = "ButtonBuchen-ChangeServiceType-showGprsDataUsage-2V5I3"
BOOK_FORM_ID   = "BaseForm-ChangeServiceType-showGprsDataUsage-2V5I3"

# Keep legacy fixed IDs as first-choice fallback, but prefer prefix selectors
# because the trailing tariff/service token can change.
BOOK_BUTTON_SELECTORS = [
    f"#{BOOK_BUTTON_ID}",
    "[id^='ButtonBuchen-ChangeServiceType-showGprsDataUsage-']",
    "a[id*='ButtonBuchen'][id*='showGprsDataUsage']",
    "a[title*='Buchen']",
    "button:has-text('Buchen')",
    "a:has-text('Buchen')",
]

BOOK_FORM_SELECTORS = [
    f"#{BOOK_FORM_ID}",
    "form[id^='BaseForm-ChangeServiceType-showGprsDataUsage-']",
    "form[id*='ChangeServiceType'][id*='showGprsDataUsage']",
]

# CSS selectors for the Aktivieren button (used in legacy query_selector path).
AKTIVIEREN_SELECTORS = [
    "[id^='ButtonAktivieren-ChangeServiceType-']",
    "a[id^='ButtonAktivieren-']",
    "a[onclick*='sendPostAndReplaceContent'][onclick*='/mytariff/invoice/changeService']",
    ".c-overlay-button-bar a.submitOnEnter[title='Aktivieren']",
    "a[title='Aktivieren']",
    "a:has-text('Aktivieren')",
]

# Locator expressions tried first — page.locator() pierces open shadow DOMs.
AKTIVIEREN_LOCATORS = [
    "a[title='Aktivieren']",
    "[id^='ButtonAktivieren-ChangeServiceType-']",
    "a[id^='ButtonAktivieren-']",
    "text=Aktivieren",
]


class BookingModule:
    def __init__(self, page: Page, telegram: TelegramNotifier):
        self.page     = page
        self.telegram = telegram
        self.captcha  = CaptchaHandler(page, telegram)
        self._trace: list[str] = []   # step-by-step trace for error reports

    def _log(self, msg: str) -> None:
        """Print and append msg to the trace."""
        print(msg)
        # Strip leading [BOOKING] tag for cleaner Telegram output.
        self._trace.append(msg.replace("[BOOKING] ", "").strip())

    def _trace_text(self) -> str:
        """Return the trace as a numbered list for Telegram."""
        lines = [f"{i+1}. {line}" for i, line in enumerate(self._trace)]
        return "\n".join(lines)

    async def book_2gb_packet(self) -> bool:
        """
        Full booking flow. Returns True on success, False on failure.
        """
        self._trace.clear()
        self._log("[BOOKING] Starting 2GB packet booking...")

        # ── Step 1: Find booking button dynamically ────────────────────────
        button, selector_used = await self._find_booking_button()

        if button is None:
            self._log("[BOOKING] ❌ Book button not found on page.")
            await self.telegram.send(
                "⚠️ *Booking button not found.*\n"
                "The page structure may have changed.\n\n"
                f"*Trace:*\n{self._trace_text()}"
            )
            await self._send_debug_screenshot("booking-button-not-found")
            return False

        self._log(f"[BOOKING] ✅ Booking button found via: `{selector_used}`")

        # Check if button is disabled
        is_disabled = await button.get_attribute("disabled")
        aria_disabled = await button.get_attribute("aria-disabled")
        if is_disabled is not None or str(aria_disabled).lower() == "true":
            self._log("[BOOKING] ⚠️ Button is disabled — attempting JS override...")
            await self.page.evaluate(
                """
                () => {
                  const candidates = Array.from(document.querySelectorAll("[id^='ButtonBuchen-ChangeServiceType-showGprsDataUsage-'], a[title*='Buchen']"));
                  for (const el of candidates) {
                    el.removeAttribute('disabled');
                    el.setAttribute('aria-disabled', 'false');
                    if (el.classList) {
                      el.classList.remove('disabled');
                    }
                  }
                }
                """
            )
            await asyncio.sleep(0.5)

        # ── Step 2: Click the booking button ──────────────────────────────
        self._log("[BOOKING] Clicking book button...")
        clicked = False
        click_method = None

        try:
            await button.click(timeout=10_000)
            clicked = True
            click_method = "standard click"
        except Exception as e:
            self._log(f"[BOOKING] Standard click failed: {type(e).__name__}")

        if not clicked:
            try:
                await button.click(force=True, timeout=5_000)
                clicked = True
                click_method = "force click"
            except Exception as e:
                self._log(f"[BOOKING] Force click failed: {type(e).__name__}")

        if not clicked:
            # Last-resort JS click using robust selector list.
            clicked = await self.page.evaluate(
                """
                () => {
                  const selectors = [
                    "#ButtonBuchen-ChangeServiceType-showGprsDataUsage-2V5I3",
                    "[id^='ButtonBuchen-ChangeServiceType-showGprsDataUsage-']",
                    "a[id*='ButtonBuchen'][id*='showGprsDataUsage']",
                    "a[title*='Buchen']"
                  ];
                  for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el) { el.click(); return true; }
                  }
                  return false;
                }
                """
            )
            if clicked:
                click_method = "JS click"

        if not clicked:
            self._log("[BOOKING] ❌ All click methods failed.")
            await self.telegram.send(
                "❌ *Booking click failed.*\n"
                "The booking button was found but could not be clicked.\n\n"
                f"*Trace:*\n{self._trace_text()}"
            )
            await self._send_debug_screenshot("booking-click-failed")
            return False

        self._log(f"[BOOKING] ✅ Book button clicked via {click_method}.")
        await asyncio.sleep(2)  # Wait for modal/cookie consent to appear

        # ── Step 3: Handle cookie consent (may be blocking the modal) ─────
        cookie_dismissed = await self._handle_cookie_consent()
        if cookie_dismissed:
            self._log("[BOOKING] ✅ Cookie consent dismissed — waiting for modal...")
            await asyncio.sleep(1)  # extra settle time after cookie dismissal

        # ── Step 4: Handle captcha if present ─────────────────────────────
        captcha_present = await self.captcha.is_captcha_present()
        if captcha_present:
            self._log("[BOOKING] 🔐 Captcha detected — sending to Telegram.")
            await self.telegram.send(
                "🔐 *Captcha appeared during booking.*\n"
                "Sending image now..."
            )

            solution = await self.captcha.solve()
            if solution is None:
                return False  # Timeout — captcha handler already notified

            entered = await self.captcha.enter_solution(solution)
            if not entered:
                self._log("[BOOKING] ❌ Could not enter captcha solution.")
                await self.telegram.send(
                    "❌ *Could not enter captcha solution.*\n"
                    "The input field was not found.\n\n"
                    f"*Trace:*\n{self._trace_text()}"
                )
                return False

            await asyncio.sleep(0.5)

        # ── Step 5: Click the Aktivieren button in the confirmation modal ──
        activation_clicked = await self._handle_activation_modal(timeout_seconds=15)

        if not activation_clicked:
            # Fallback: try generic confirm selectors (e.g. after captcha flows).
            confirmed = await self._confirm_booking()
            if not confirmed:
                self._log("[BOOKING] ⚠️ No confirm action found — proceeding to verify.")

        # ── Step 6: Verify booking success ────────────────────────────────
        success = await self._verify_success()
        if not success:
            await self._send_debug_screenshot("booking-verify-failed")
        return success

    async def _find_booking_button(self):
        """Return first visible booking button and selector used."""
        for selector in BOOK_BUTTON_SELECTORS:
            try:
                candidate = await self.page.query_selector(selector)
                if candidate and await candidate.is_visible():
                    return candidate, selector
            except Exception:
                continue
        return None, None

    async def _handle_cookie_consent(self) -> bool:
        """Accept cookie consent popup when it appears."""
        cookie_selectors = [
            "#consent_wall_optin",
            "button#consent_wall_optin",
            "button:has-text('Bestätigen')",
        ]

        for selector in cookie_selectors:
            try:
                btn = await self.page.query_selector(selector)
                if btn and await btn.is_visible():
                    print(f"[BOOKING] Cookie consent detected via: {selector}")
                    await btn.click()
                    await asyncio.sleep(1)
                    return True
            except Exception:
                continue

        return False

    async def _handle_activation_modal(self, timeout_seconds: int = 15) -> bool:
        """
        Click the purple Aktivieren button in the SIM24 confirmation modal.

        The button lives inside <dialog is="c-overlay">, a web component whose
        shadow DOM blocks query_selector. We therefore try three approaches in
        order of preference:

          1. page.locator() — pierces open shadow DOMs natively.
          2. Shadow-piercing recursive JS click.
          3. Legacy query_selector path (non-shadow fallback).
        """
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        attempt  = 0

        while asyncio.get_event_loop().time() < deadline:
            attempt += 1

            # ── Approach 1: page.locator() (shadow-DOM-aware) ──────────────
            for loc_sel in AKTIVIEREN_LOCATORS:
                try:
                    loc = self.page.locator(loc_sel)
                    count = await loc.count()
                    if count == 0:
                        continue
                    if not await loc.first.is_visible():
                        continue

                    self._log(
                        f"[BOOKING] ✅ Aktivieren found via locator `{loc_sel}` "
                        f"(attempt {attempt})."
                    )

                    # Try a normal locator click first.
                    try:
                        await loc.first.click(timeout=5_000)
                        self._log("[BOOKING] ✅ Aktivieren clicked (locator click).")
                        await asyncio.sleep(3)
                        return True
                    except Exception as e:
                        self._log(
                            f"[BOOKING] Locator click blocked ({type(e).__name__}) "
                            "— trying force click..."
                        )

                    # Force click bypasses pointer-event interception.
                    try:
                        await loc.first.click(force=True, timeout=5_000)
                        self._log("[BOOKING] ✅ Aktivieren clicked (force click).")
                        await asyncio.sleep(3)
                        return True
                    except Exception as e:
                        self._log(
                            f"[BOOKING] Force click also blocked ({type(e).__name__}) "
                            "— falling back to shadow-piercing JS..."
                        )

                    # Shadow-piercing JS: walks shadow roots to find the element,
                    # then calls sendPostAndReplaceContent() or el.click().
                    activated = await self.page.evaluate(
                        """
                        (sel) => {
                          function queryShadow(root, selector) {
                            const el = root.querySelector(selector);
                            if (el) return el;
                            for (const node of root.querySelectorAll('*')) {
                              if (node.shadowRoot) {
                                const found = queryShadow(node.shadowRoot, selector);
                                if (found) return found;
                              }
                            }
                            return null;
                          }
                          const el = queryShadow(document, sel);
                          if (!el) return 'not-found';
                          const onclick = el.getAttribute('onclick') || '';
                          const m = onclick.match(
                            /sendPostAndReplaceContent\("([^"]+)",\s*"([^"]+)"/
                          );
                          if (m && typeof sendPostAndReplaceContent === 'function') {
                            sendPostAndReplaceContent(m[1], m[2], true);
                            return 'sendPost';
                          }
                          el.click();
                          return 'js-click';
                        }
                        """,
                        loc_sel,
                    )
                    if activated and activated != "not-found":
                        self._log(
                            f"[BOOKING] ✅ Aktivieren triggered via JS ({activated})."
                        )
                        await asyncio.sleep(3)
                        return True
                    else:
                        self._log(
                            f"[BOOKING] JS shadow-pierce result: `{activated}` "
                            f"for selector `{loc_sel}`."
                        )

                except Exception as e:
                    self._log(
                        f"[BOOKING] Locator path error for `{loc_sel}`: "
                        f"{type(e).__name__}: {e}"
                    )
                    continue

            # ── Approach 2: Legacy query_selector path ─────────────────────
            for selector in AKTIVIEREN_SELECTORS:
                try:
                    btn = await self.page.query_selector(selector)
                    if btn is None or not await btn.is_visible():
                        continue

                    self._log(
                        f"[BOOKING] ✅ Aktivieren found via query_selector `{selector}`."
                    )

                    js = (
                        "(sel) => {"
                        "  const el = document.querySelector(sel);"
                        "  if (!el) return 'not-found';"
                        "  const onclick = el.getAttribute('onclick') || '';"
                        "  const m = onclick.match("
                        "    /sendPostAndReplaceContent\\(\"([^\"]+)\",\\s*\"([^\"]+)\"/"
                        "  );"
                        "  if (m && typeof sendPostAndReplaceContent === 'function') {"
                        "    sendPostAndReplaceContent(m[1], m[2], true);"
                        "    return 'sendPost';"
                        "  }"
                        "  const urlM = onclick.match(/\"(\\/mytariff\\/invoice\\/[^\"]+)\"/);"
                        "  const frmM = onclick.match(/,\\s*\"(BaseForm-[^\"]+)\"/);"
                        "  if (urlM && frmM && typeof sendPostAndReplaceContent === 'function') {"
                        "    sendPostAndReplaceContent(urlM[1], frmM[1], true);"
                        "    return 'sendPost-fallback';"
                        "  }"
                        "  el.click();"
                        "  return 'js-click';"
                        "}"
                    )
                    result = await self.page.evaluate(js, selector)
                    if result and result != "not-found":
                        self._log(
                            f"[BOOKING] ✅ Aktivieren triggered via query_selector JS "
                            f"({result})."
                        )
                        await asyncio.sleep(3)
                        return True

                except Exception as e:
                    self._log(
                        f"[BOOKING] query_selector path error for `{selector}`: "
                        f"{type(e).__name__}: {e}"
                    )
                    continue

            await asyncio.sleep(0.5)

        self._log(
            f"[BOOKING] ❌ Aktivieren button not found after {timeout_seconds}s "
            f"({attempt} poll attempts)."
        )
        return False

    async def _confirm_booking(self) -> bool:
        """
        Fallback: look for a generic confirm button (used in captcha flows).
        Does NOT include 'Buchen' — that is the initial trigger, not a confirm.
        """
        confirm_selectors = [
            "a[onclick*='submitForm'][onclick*='ChangeServiceType']",
            "a.c-button[onclick*='submitForm']",
            "a.c-button[title='Bestätigen']",
            "a.c-button[title='Bestellen']",
            "button[type='submit']",
            "input[type='submit']",
            ".btn-submit",
        ]

        for selector in confirm_selectors:
            try:
                btn = await self.page.query_selector(selector)
                if btn and await btn.is_visible():
                    self._log(f"[BOOKING] Found fallback confirm button: `{selector}`")
                    await btn.click()
                    await asyncio.sleep(2)
                    return True
            except Exception:
                continue

        # Try submitting the form directly via JavaScript.
        for selector in BOOK_FORM_SELECTORS:
            try:
                form_exists = await self.page.query_selector(selector)
                if form_exists:
                    self._log(f"[BOOKING] Submitting form via JS: `{selector}`")
                    submitted = await self.page.evaluate(
                        """
                        (sel) => {
                          const f = document.querySelector(sel);
                          if (!f) return false;
                          f.submit();
                          return true;
                        }
                        """,
                        selector,
                    )
                    if submitted:
                        await asyncio.sleep(2)
                        return True
            except Exception as e:
                self._log(f"[BOOKING] JS form submit failed for `{selector}`: {e}")

        return False

    async def _verify_success(self) -> bool:
        """
        Checks for success/failure indicators after booking activation.
        Waits for the AJAX response to settle first.
        """
        try:
            await self.page.wait_for_load_state("networkidle", timeout=10_000)
            self._log("[BOOKING] Network idle after activation.")
        except Exception:
            self._log("[BOOKING] Network-idle timeout — sleeping 3s instead.")
            await asyncio.sleep(3)

        try:
            current_url = self.page.url
            page_content = await self.page.content()
            page_content_lower = page_content.lower()

            self._log(f"[BOOKING] Verifying at URL: `{current_url}`")

            success_keywords = [
                "erfolgreich",
                "gebucht",
                "buchung bestaetigt",
                "buchung bestätigt",
                "bestellung erfolgreich",
                "successfully",
            ]
            for keyword in success_keywords:
                if keyword in page_content_lower:
                    self._log(f"[BOOKING] ✅ Success keyword found: `{keyword}`")
                    return True

            if "success" in current_url.lower() or "bestaetigung" in current_url.lower():
                self._log("[BOOKING] ✅ Success URL detected.")
                return True

            failure_keywords = [
                "fehlgeschlagen",
                "nicht moeglich",
                "nicht möglich",
                "ungueltig",
                "ungültig",
                "ein fehler ist aufgetreten",
            ]
            for keyword in failure_keywords:
                if keyword in page_content_lower:
                    self._log(f"[BOOKING] ❌ Failure keyword found: `{keyword}`")
                    await self.telegram.send(
                        f"❌ *Booking failed.*\nDetected: `{keyword}`\n\n"
                        f"*Trace:*\n{self._trace_text()}"
                    )
                    return False

        except Exception as e:
            self._log(f"[BOOKING] ❌ Verification error: {type(e).__name__}: {e}")

        # Inconclusive — report the full trace.
        self._log("[BOOKING] ❌ Could not determine outcome from page content.")
        await self.telegram.send(
            "⚠️ *Booking submitted but outcome is unclear.*\n"
            "Screenshot follows. Bot will retry if still below threshold.\n\n"
            f"*Trace:*\n{self._trace_text()}"
        )
        return False

    async def _send_debug_screenshot(self, reason: str) -> None:
        """Best-effort screenshot for troubleshooting booking failures."""
        try:
            shot = await self.page.screenshot(full_page=True)
            await self.telegram.send_photo(
                image_bytes=shot,
                caption=(
                    f"🧩 *Booking debug screenshot*\n"
                    f"Reason: `{reason}`\n"
                    f"URL: `{self.page.url}`"
                ),
            )
        except Exception as e:
            print(f"[BOOKING] Failed to send debug screenshot: {e}")
