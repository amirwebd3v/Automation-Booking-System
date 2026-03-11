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

        # ── Step 1: Find booking button dynamically ────────────────────────
        button, selector_used = await self._find_booking_button()

        if button is None:
            print("[BOOKING] Book button not found on page.")
            await self.telegram.send(
                "⚠️ *Booking button not found.*\n"
                "The page structure may have changed."
            )
            await self._send_debug_screenshot("booking-button-not-found")
            return False

        print(f"[BOOKING] Booking button found via selector: {selector_used}")

        # Check if button is disabled
        is_disabled = await button.get_attribute("disabled")
        aria_disabled = await button.get_attribute("aria-disabled")
        if is_disabled is not None or str(aria_disabled).lower() == "true":
            print("[BOOKING] Button looks disabled. Attempting JS override...")
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
        print("[BOOKING] Clicking book button...")
        clicked = False
        try:
            await button.click(timeout=10_000)
            clicked = True
        except Exception as e:
            print(f"[BOOKING] Standard click failed: {e}")

        if not clicked:
            try:
                await button.click(force=True, timeout=10_000)
                clicked = True
            except Exception as e:
                print(f"[BOOKING] Forced click failed: {e}")

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
                    if (el) {
                      el.click();
                      return true;
                    }
                  }
                  return false;
                }
                """
            )

        if not clicked:
            await self.telegram.send(
                "❌ *Booking click failed.*\n"
                "The booking button was found but could not be clicked."
            )
            await self._send_debug_screenshot("booking-click-failed")
            return False

        await asyncio.sleep(2)  # Wait for any modal/captcha to appear

        # Some runs show cookie consent before activation dialog.
        await self._handle_cookie_consent()

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

        # Primary confirmation path on SIM24: activation modal button.
        activation_clicked = await self._handle_activation_modal(timeout_seconds=12)

        # Fallback if activation modal is not present in this run.
        if not activation_clicked:
            confirmed = await self._confirm_booking()
            if not confirmed:
                print("[BOOKING] No explicit confirm action found. Will continue with verification.")

        # ── Step 5: Verify booking success ────────────────────────────────
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

    async def _handle_activation_modal(self, timeout_seconds: int = 10) -> bool:
        """Click the activation button shown in booking confirmation modal."""
        activation_selectors = [
            "[id^='ButtonAktivieren-ChangeServiceType-']",
            "a[id^='ButtonAktivieren-']",
            "a[onclick*='sendPostAndReplaceContent'][onclick*='/mytariff/invoice/changeService']",
            ".c-overlay-button-bar a.submitOnEnter[title='Aktivieren']",
            "a[title='Aktivieren']",
            "a:has-text('Aktivieren')",
        ]

        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while asyncio.get_event_loop().time() < deadline:
            # Best path for this portal: call the exact JS action wired in onclick.
            triggered_post = await self._trigger_activation_post_action()
            if triggered_post:
                print("[BOOKING] Activation submitted via sendPostAndReplaceContent().")
                return True

            for selector in activation_selectors:
                try:
                    btn = await self.page.query_selector(selector)
                    if btn and await btn.is_visible():
                        print(f"[BOOKING] Activation modal detected via: {selector}")
                        try:
                            await btn.click(timeout=10_000)
                            await asyncio.sleep(2)
                            return True
                        except Exception as e:
                            print(f"[BOOKING] Activation standard click failed: {e}")
                            try:
                                await btn.click(force=True, timeout=10_000)
                                await asyncio.sleep(2)
                                return True
                            except Exception as e2:
                                print(f"[BOOKING] Activation forced click failed: {e2}")
                except Exception:
                    continue

            # JS fallback for dynamic modal button wiring.
            try:
                clicked = await self.page.evaluate(
                    """
                    () => {
                      const selectors = [
                        "[id^='ButtonAktivieren-ChangeServiceType-']",
                        "a[id^='ButtonAktivieren-']",
                        "a[onclick*='sendPostAndReplaceContent'][onclick*='/mytariff/invoice/changeService']",
                        ".c-overlay-button-bar a.submitOnEnter[title='Aktivieren']",
                        "a[title='Aktivieren']"
                      ];
                      for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el) {
                          el.click();
                          return true;
                        }
                      }
                      return false;
                    }
                    """
                )
                if clicked:
                    print("[BOOKING] Activation clicked via JS fallback.")
                    await asyncio.sleep(2)
                    return True
            except Exception:
                pass

            await asyncio.sleep(0.5)

        print("[BOOKING] Activation modal not found within timeout.")
        return False

        async def _trigger_activation_post_action(self) -> bool:
                """
                Execute the exact portal activation action from the button's onclick:
                    sendPostAndReplaceContent('/mytariff/invoice/changeService', formId, true)
                """
                try:
                        result = await self.page.evaluate(
                                r"""
                                () => {
                                    const candidates = [
                                        "[id^='ButtonAktivieren-ChangeServiceType-']",
                                        "a[id^='ButtonAktivieren-']",
                                        "a[onclick*='sendPostAndReplaceContent'][onclick*='/mytariff/invoice/changeService']",
                                        ".c-overlay-button-bar a.submitOnEnter[title='Aktivieren']",
                                        "a[title='Aktivieren']"
                                    ];

                                    let btn = null;
                                    for (const sel of candidates) {
                                        const el = document.querySelector(sel);
                                        if (el) {
                                            btn = el;
                                            break;
                                        }
                                    }
                                    if (!btn) return { triggered: false, reason: "no-button" };

                                    const onclick = btn.getAttribute("onclick") || "";
                                    const fn = window.sendPostAndReplaceContent;
                                    const match = onclick.match(/sendPostAndReplaceContent\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*,\s*(true|false)\s*\)/i);

                                    if (typeof fn === "function" && match) {
                                        const url = match[1];
                                        const formId = match[2];
                                        const closeOverlay = match[3].toLowerCase() === "true";
                                        fn(url, formId, closeOverlay);
                                        return { triggered: true, reason: "fn-call" };
                                    }

                                    btn.click();
                                    return { triggered: true, reason: "fallback-click" };
                                }
                                """
                        )

                        if not result or not result.get("triggered"):
                                return False

                        try:
                                await self.page.wait_for_response(
                                        lambda r: "/mytariff/invoice/changeService" in r.url and r.request.method.upper() in {"POST", "GET"},
                                        timeout=8_000,
                                )
                        except Exception:
                                # The site may update via XHR or cached flow; short wait as fallback.
                                await asyncio.sleep(2)

                        return True
                except Exception as e:
                        print(f"[BOOKING] Activation post-action failed: {e}")
                        return False

    async def _confirm_booking(self) -> bool:
        """
        Looks for a confirmation button or form submit after captcha.
        Returns True if found and clicked, False otherwise.
        """
        # Common selectors for confirmation submit buttons
        confirm_selectors = [
            "a[onclick*='submitForm'][onclick*='ChangeServiceType']",
            "a.c-button[onclick*='submitForm']",
            "a.c-button[title='Buchen']",
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
                    print(f"[BOOKING] Found confirm button: {selector}")
                    await btn.click()
                    await asyncio.sleep(2)
                    return True
            except Exception:
                continue

        # Try submitting the form directly via JavaScript
        for selector in BOOK_FORM_SELECTORS:
            try:
                form_exists = await self.page.query_selector(selector)
                if form_exists:
                    print(f"[BOOKING] Submitting form via JavaScript: {selector}")
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
                print(f"[BOOKING] JS form submit failed for {selector}: {e}")

        return False

    async def _verify_success(self) -> bool:
        """
        Checks for success indicators on the page after booking attempt.
        """
        await asyncio.sleep(2)

        try:
            page_content = await self.page.content()
            page_content_lower = page_content.lower()

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
                    print(f"[BOOKING] Success keyword found: '{keyword}'")
                    return True

            failure_keywords = [
                "fehlgeschlagen",
                "nicht moeglich",
                "nicht möglich",
                "ungueltig",
                "ungültig",
                "captcha",
                "fehler",
                "ein fehler ist aufgetreten",
            ]
            for keyword in failure_keywords:
                if keyword in page_content_lower:
                    print(f"[BOOKING] Failure keyword found: '{keyword}'")
                    await self.telegram.send(
                        f"❌ *Booking seems to have failed.*\nDetected keyword: `{keyword}`"
                    )
                    return False

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

        # Ambiguous result: mark as failed so next cycle can retry instead of
        # incorrectly assuming success.
        await self.telegram.send(
            "⚠️ *Booking submitted but could not be verified.*\n"
            "Please check screenshot and account. Bot will retry next cycle if still below threshold."
        )
        return False

    async def _send_debug_screenshot(self, reason: str) -> None:
        """Best-effort screenshot for troubleshooting booking failures."""
        try:
            shot = await self.page.screenshot(full_page=True)
            await self.telegram.send_photo(
                image_bytes=shot,
                caption=f"🧩 *Booking debug screenshot*\nReason: `{reason}`\nURL: `{self.page.url}`",
            )
        except Exception as e:
            print(f"[BOOKING] Failed to send debug screenshot: {e}")
