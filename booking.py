"""
Booking Module
──────────────
Handles the 2GB data packet booking flow.

From the HTML analysis:
  Button ID:    ButtonBuchen-ChangeServiceType-showGprsDataUsage-*
  Form ID:      BaseForm-ChangeServiceType-showGprsDataUsage-*
  Service code: V5I3

Flow:
  1. Set up a listener for the getChangeServiceInfo AJAX response.
  2. Click the Buchen button (force if standard click is blocked by overlay).
  3. Parse the captured response HTML to extract the Aktivieren button's
     sendPostAndReplaceContent(url, formId) call.
  4. Execute that call directly — no DOM search needed, closed shadow DOM safe.
  5. Verify success via page content keywords.
"""

import asyncio
import re
from playwright.async_api import Page
from telegram_notify import TelegramNotifier
from captcha_handler import CaptchaHandler


BOOK_BUTTON_SELECTORS = [
    "[id^='ButtonBuchen-ChangeServiceType-showGprsDataUsage-']",
    "a[id*='ButtonBuchen'][id*='showGprsDataUsage']",
    "a[title='Buchen']",
    "button:has-text('Buchen')",
    "a:has-text('Buchen')",
]

BOOK_FORM_SELECTOR = "[id^='BaseForm-ChangeServiceType-showGprsDataUsage-']"

# Activation URL used when we cannot extract it from the modal response.
FALLBACK_ACTIVATION_URL = "/mytariff/invoice/changeService"


class BookingModule:
    def __init__(self, page: Page, telegram: TelegramNotifier):
        self.page     = page
        self.telegram = telegram
        self.captcha  = CaptchaHandler(page, telegram)
        self._trace: list[str] = []

    def _log(self, msg: str) -> None:
        print(msg)
        self._trace.append(msg.replace("[BOOKING] ", "").strip())

    def _trace_text(self) -> str:
        return "\n".join(f"{i+1}. {line}" for i, line in enumerate(self._trace))

    async def book_2gb_packet(self) -> bool:
        self._trace.clear()
        self._log("[BOOKING] Starting 2GB packet booking...")

        # ── Step 1: Find booking button ────────────────────────────────────
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

        # Remove disabled state if set.
        is_disabled  = await button.get_attribute("disabled")
        aria_disabled = await button.get_attribute("aria-disabled")
        if is_disabled is not None or str(aria_disabled).lower() == "true":
            self._log("[BOOKING] ⚠️ Button disabled — applying JS override...")
            await self.page.evaluate(
                """
                () => {
                  for (const el of document.querySelectorAll(
                      "[id^='ButtonBuchen-ChangeServiceType-showGprsDataUsage-'], a[title='Buchen']"
                  )) {
                    el.removeAttribute('disabled');
                    el.setAttribute('aria-disabled', 'false');
                    el.classList && el.classList.remove('disabled');
                  }
                }
                """
            )
            await asyncio.sleep(0.5)

        # ── Step 2: Start listening for the getChangeServiceInfo response ──
        # We must create the task BEFORE clicking so we don't miss the response.
        info_response_task = asyncio.create_task(
            self.page.wait_for_response(
                lambda r: "getChangeServiceInfo" in r.url,
                timeout=25_000,
            )
        )

        # ── Step 3: Click the booking button ──────────────────────────────
        self._log("[BOOKING] Clicking book button...")
        clicked = False
        click_method = None

        try:
            await button.click(timeout=10_000)
            clicked = True
            click_method = "standard click"
        except Exception as e:
            self._log(f"[BOOKING] Standard click blocked ({type(e).__name__}) — trying force click...")

        if not clicked:
            try:
                await button.click(force=True, timeout=5_000)
                clicked = True
                click_method = "force click"
            except Exception as e:
                self._log(f"[BOOKING] Force click failed ({type(e).__name__}) — trying JS click...")

        if not clicked:
            clicked = await self.page.evaluate(
                """
                () => {
                  const selectors = [
                    "[id^='ButtonBuchen-ChangeServiceType-showGprsDataUsage-']",
                    "a[id*='ButtonBuchen'][id*='showGprsDataUsage']",
                    "a[title='Buchen']",
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
            info_response_task.cancel()
            self._log("[BOOKING] ❌ All click methods failed.")
            await self.telegram.send(
                "❌ *Booking click failed.*\n\n"
                f"*Trace:*\n{self._trace_text()}"
            )
            await self._send_debug_screenshot("booking-click-failed")
            return False

        self._log(f"[BOOKING] ✅ Book button clicked via {click_method}.")
        await asyncio.sleep(2)

        # ── Step 4: Dismiss cookie consent (may appear after click) ───────
        cookie_dismissed = await self._handle_cookie_consent()
        if cookie_dismissed:
            self._log("[BOOKING] ✅ Cookie consent dismissed.")
            await asyncio.sleep(1)

        # ── Step 5: Handle captcha if present ─────────────────────────────
        if await self.captcha.is_captcha_present():
            self._log("[BOOKING] 🔐 Captcha detected.")
            await self.telegram.send("🔐 *Captcha appeared during booking.*\nSending image now...")
            solution = await self.captcha.solve()
            if solution is None:
                return False
            if not await self.captcha.enter_solution(solution):
                self._log("[BOOKING] ❌ Could not enter captcha solution.")
                await self.telegram.send(
                    "❌ *Could not enter captcha solution.*\n\n"
                    f"*Trace:*\n{self._trace_text()}"
                )
                return False
            await asyncio.sleep(0.5)

        # ── Step 6: Activate via captured AJAX response ───────────────────
        activation_clicked = await self._activate_from_response(info_response_task)

        # ── Step 7: Fallback — direct changeService call ──────────────────
        if not activation_clicked:
            self._log(
                "[BOOKING] ⚠️ Response-based activation failed — "
                "trying direct changeService call..."
            )
            activation_clicked = await self._activate_directly()

        if not activation_clicked:
            self._log("[BOOKING] ⚠️ No confirm action succeeded — proceeding to verify anyway.")

        # ── Step 8: Verify booking success ────────────────────────────────
        success = await self._verify_success()
        if not success:
            await self._send_debug_screenshot("booking-verify-failed")
        return success

    # ── Helpers ────────────────────────────────────────────────────────────

    async def _find_booking_button(self):
        for selector in BOOK_BUTTON_SELECTORS:
            try:
                candidate = await self.page.query_selector(selector)
                if candidate and await candidate.is_visible():
                    return candidate, selector
            except Exception:
                continue
        return None, None

    async def _handle_cookie_consent(self) -> bool:
        for selector in ["#consent_wall_optin", "button#consent_wall_optin",
                         "button:has-text('Bestätigen')"]:
            try:
                btn = await self.page.query_selector(selector)
                if btn and await btn.is_visible():
                    print(f"[BOOKING] Cookie consent via: {selector}")
                    await btn.click()
                    await asyncio.sleep(1)
                    return True
            except Exception:
                continue
        return False

    async def _activate_from_response(
        self, response_task: asyncio.Task
    ) -> bool:
        """
        Await the getChangeServiceInfo AJAX response, parse its HTML to find
        the Aktivieren button's sendPostAndReplaceContent(url, formId) call,
        then execute it directly.

        This approach is closed-shadow-DOM-safe because it never needs to find
        the Aktivieren button in the rendered DOM at all.
        """
        try:
            self._log("[BOOKING] Waiting for getChangeServiceInfo response...")
            response = await asyncio.wait_for(response_task, timeout=20)
            status   = response.status
            html     = await response.text()
            self._log(
                f"[BOOKING] ✅ getChangeServiceInfo response received "
                f"(HTTP {status}, {len(html)} chars)."
            )
        except asyncio.TimeoutError:
            self._log("[BOOKING] ❌ getChangeServiceInfo response timed out.")
            response_task.cancel()
            return False
        except Exception as e:
            self._log(f"[BOOKING] ❌ getChangeServiceInfo capture error: {e}")
            response_task.cancel()
            return False

        # Parse the modal HTML for the Aktivieren button onclick.
        # Pattern: sendPostAndReplaceContent('/mytariff/invoice/changeService', 'BaseForm-...', true)
        m = re.search(
            r"sendPostAndReplaceContent\s*\(\s*['\"]([^'\"]+)['\"],\s*['\"]([^'\"]+)['\"]",
            html,
        )
        if m:
            activation_url = m.group(1)
            form_id        = m.group(2)
            self._log(
                f"[BOOKING] ✅ Extracted activation: url=`{activation_url}` "
                f"formId=`{form_id}`"
            )
        else:
            # No sendPostAndReplaceContent found — fall back to known URL + page form.
            self._log(
                "[BOOKING] ⚠️ Could not parse activation URL from response — "
                f"using fallback `{FALLBACK_ACTIVATION_URL}`."
            )
            activation_url = FALLBACK_ACTIVATION_URL
            form_id        = None  # will be resolved in JS below

        result = await self.page.evaluate(
            """
            ([url, formId]) => {
              // Resolve formId from page if not supplied.
              if (!formId) {
                const f = document.querySelector(
                  "[id^='BaseForm-ChangeServiceType-showGprsDataUsage-']"
                );
                if (!f) return 'no-form';
                formId = f.id;
              }
              if (typeof sendPostAndReplaceContent === 'function') {
                sendPostAndReplaceContent(url, formId, true);
                return 'sendPost';
              }
              // Final fallback: submit form directly to the activation URL.
              const form = document.getElementById(formId)
                || document.querySelector(
                     "[id^='BaseForm-ChangeServiceType-showGprsDataUsage-']"
                   );
              if (!form) return 'no-form';
              form.action = url;
              form.submit();
              return 'form-submit';
            }
            """,
            [activation_url, form_id],
        )

        if result in ("sendPost", "form-submit"):
            self._log(f"[BOOKING] ✅ Activation triggered ({result}).")
            await asyncio.sleep(4)
            return True

        self._log(f"[BOOKING] ❌ Activation JS returned: `{result}`")
        return False

    async def _activate_directly(self) -> bool:
        """
        Last-resort: call sendPostAndReplaceContent for the changeService URL
        using the form that is already on the page (has CSRF token + service code).
        Skips the getChangeServiceInfo intermediate step.
        """
        result = await self.page.evaluate(
            """
            (url) => {
              const form = document.querySelector(
                "[id^='BaseForm-ChangeServiceType-showGprsDataUsage-']"
              );
              if (!form) return 'no-form';
              if (typeof sendPostAndReplaceContent === 'function') {
                sendPostAndReplaceContent(url, form.id, true);
                return 'sendPost-direct';
              }
              form.action = url;
              form.submit();
              return 'form-submit-direct';
            }
            """,
            FALLBACK_ACTIVATION_URL,
        )
        if result in ("sendPost-direct", "form-submit-direct"):
            self._log(f"[BOOKING] ✅ Direct activation triggered ({result}).")
            await asyncio.sleep(4)
            return True
        self._log(f"[BOOKING] ❌ Direct activation JS returned: `{result}`")
        return False

    async def _verify_success(self) -> bool:
        try:
            await self.page.wait_for_load_state("networkidle", timeout=10_000)
            self._log("[BOOKING] Network idle after activation.")
        except Exception:
            self._log("[BOOKING] Network-idle timeout — sleeping 4s.")
            await asyncio.sleep(4)

        try:
            current_url   = self.page.url
            content_lower = (await self.page.content()).lower()
            self._log(f"[BOOKING] Verifying at URL: `{current_url}`")

            for kw in ["erfolgreich", "gebucht", "buchung bestaetigt",
                       "buchung bestätigt", "bestellung erfolgreich", "successfully"]:
                if kw in content_lower:
                    self._log(f"[BOOKING] ✅ Success keyword: `{kw}`")
                    return True

            if "success" in current_url.lower() or "bestaetigung" in current_url.lower():
                self._log("[BOOKING] ✅ Success URL detected.")
                return True

            for kw in ["fehlgeschlagen", "nicht moeglich", "nicht möglich",
                       "ungueltig", "ungültig", "ein fehler ist aufgetreten"]:
                if kw in content_lower:
                    self._log(f"[BOOKING] ❌ Failure keyword: `{kw}`")
                    await self.telegram.send(
                        f"❌ *Booking failed.*\nDetected: `{kw}`\n\n"
                        f"*Trace:*\n{self._trace_text()}"
                    )
                    return False

        except Exception as e:
            self._log(f"[BOOKING] ❌ Verification error: {type(e).__name__}: {e}")

        self._log("[BOOKING] ❌ Outcome unclear from page content.")
        await self.telegram.send(
            "⚠️ *Booking submitted but outcome is unclear.*\n"
            "Screenshot follows. Bot will retry if still below threshold.\n\n"
            f"*Trace:*\n{self._trace_text()}"
        )
        return False

    async def _send_debug_screenshot(self, reason: str) -> None:
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
