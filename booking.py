"""
Booking Module
──────────────
Handles the 2GB data packet booking flow.

From the HTML analysis:
  Button ID:    ButtonBuchen-ChangeServiceType-showGprsDataUsage-*
  Form ID:      BaseForm-ChangeServiceType-showGprsDataUsage-*
  Service code: V5I3

Flow:
  1. Open an expect_response listener for getChangeServiceInfo.
  2. Click the Buchen button inside that listener context.
  3. Parse the captured AJAX response to extract the Aktivieren URL.
  4. Execute sendPostAndReplaceContent(url, formId) directly.
  5. Verify success via page content keywords.
"""

import asyncio
import re
from playwright.async_api import Page
from telegram_notify import TelegramNotifier
from captcha_handler import CaptchaHandler, MODAL_SELECTOR, SPINNER_SELECTORS


BOOK_BUTTON_SELECTORS = [
    "[id^='ButtonBuchen-ChangeServiceType-showGprsDataUsage-']",
    "a[id*='ButtonBuchen'][id*='showGprsDataUsage']",
    "a[title='Buchen']",
    "button:has-text('Buchen')",
    "a:has-text('Buchen')",
]

BOOK_FORM_SELECTOR    = "[id^='BaseForm-ChangeServiceType-showGprsDataUsage-']"
FALLBACK_ACTIVATE_URL = "/mytariff/invoice/changeService"


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
            self._log("[BOOKING] ❌ Book button not found.")
            return False

        self._log(f"[BOOKING] ✅ Booking button found via: `{selector_used}`")

        # Remove disabled state if present.
        is_disabled   = await button.get_attribute("disabled")
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

        # ── Steps 2–3: Click Buchen while capturing getChangeServiceInfo ───
        # expect_response must wrap the click so the listener is active when
        # the AJAX call fires.  Timeout is generous (30 s) to survive a slow
        # standard-click timeout + force-click attempt.
        clicked      = False
        click_method = None
        modal_html   = None

        # expect_response timeout must cover all fallback click attempts:
        # standard click (10s) + force click (5s) + response arrival (~15s) = 60s.
        try:
            async with self.page.expect_response(
                lambda r: "getChangeServiceInfo" in r.url,
                timeout=60_000,
            ) as resp_info:

                try:
                    await button.click(timeout=10_000)
                    clicked = True
                    click_method = "standard click"
                except Exception as e:
                    self._log(
                        f"[BOOKING] Standard click blocked ({type(e).__name__}) "
                        "— trying force click..."
                    )

                if not clicked:
                    try:
                        await button.click(force=True, timeout=5_000)
                        clicked = True
                        click_method = "force click"
                    except Exception as e:
                        self._log(
                            f"[BOOKING] Force click failed ({type(e).__name__}) "
                            "— trying JS click..."
                        )

                if not clicked:
                    clicked = await self.page.evaluate(
                        """
                        () => {
                          const sels = [
                            "[id^='ButtonBuchen-ChangeServiceType-showGprsDataUsage-']",
                            "a[id*='ButtonBuchen'][id*='showGprsDataUsage']",
                            "a[title='Buchen']",
                          ];
                          for (const s of sels) {
                            const el = document.querySelector(s);
                            if (el) { el.click(); return true; }
                          }
                          return false;
                        }
                        """
                    )
                    if clicked:
                        click_method = "JS click"

            # Collect the AJAX response captured by the context manager.
            try:
                info_response = await resp_info.value
                modal_html    = await info_response.text()
                self._log(
                    f"[BOOKING] ✅ getChangeServiceInfo response: "
                    f"HTTP {info_response.status}, {len(modal_html)} chars."
                )
            except Exception as e:
                self._log(f"[BOOKING] ⚠️ Could not capture getChangeServiceInfo response: {e}")

        except Exception as e:
            # expect_response timed out — proceed with fallback activation.
            self._log(f"[BOOKING] ⚠️ expect_response timed out ({type(e).__name__}) — proceeding without modal HTML.")

        # Fail fast if nothing was clicked.
        if not clicked:
            self._log("[BOOKING] ❌ All click methods failed.")
            return False

        self._log(f"[BOOKING] ✅ Book button clicked via {click_method}.")

        await self._wait_for_booking_modal(timeout_seconds=10)

        # ── Step 4: Dismiss cookie consent if it appeared ─────────────────
        if await self._handle_cookie_consent():
            self._log("[BOOKING] ✅ Cookie consent dismissed.")
            await self._wait_for_booking_modal(timeout_seconds=10)

        # ── Step 5: Handle captcha if present ─────────────────────────
        captcha_handled = False
        if await self.captcha.is_captcha_present():
            self._log("[BOOKING] 🔐 Captcha detected.")
            captcha_ok = await self.captcha.solve_with_retry(max_attempts=3)
            if not captcha_ok:
                self._log("[BOOKING] ❌ Captcha solving failed.")
                return False
            self._log("[BOOKING] ✅ Captcha solved and Aktivieren clicked.")
            captcha_handled = True

        # ── Step 6: Activate via parsed modal HTML ─────────────────────
        # Skipped when captcha handler already submitted via Aktivieren button.
        activation_clicked = captcha_handled

        if not captcha_handled:
            activation_clicked = await self._activate_from_modal()

            if not activation_clicked and modal_html:
                activation_clicked = await self._activate_from_html(modal_html)

            # ── Step 7: Fallback — call changeService directly ─────────
            if not activation_clicked:
                self._log(
                    "[BOOKING] ⚠️ HTML-based activation failed — "
                    "trying direct changeService call..."
                )
                activation_clicked = await self._activate_directly()

            if not activation_clicked:
                self._log("[BOOKING] ⚠️ No confirm action succeeded — proceeding to verify.")

        # ── Step 8: Check for captcha that appeared in the server's response ─
        # This covers the case where the server returns a captcha challenge
        # after the form is submitted (e.g. direct changeService call), which
        # is different from the pre-activation captcha caught in Step 5.
        # Wait for any loading spinner before checking DOM state.
        if not captcha_handled:
            await self._wait_for_loading()
        if not captcha_handled and await self.captcha.is_captcha_present():
            self._log("[BOOKING] 🔐 Captcha appeared in server response after activation.")
            captcha_ok = await self.captcha.solve_with_retry(max_attempts=3)
            if not captcha_ok:
                self._log("[BOOKING] ❌ Post-activation captcha failed.")
                return False
            self._log("[BOOKING] ✅ Post-activation captcha solved and Aktivieren clicked.")

        # ── Step 9: Verify booking success ────────────────────────────────
        return await self._verify_success()

    # ── Helpers ────────────────────────────────────────────────────────────

    async def _find_booking_button(self):
        for sel in BOOK_BUTTON_SELECTORS:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    return el, sel
            except Exception:
                continue
        return None, None

    async def _handle_cookie_consent(self) -> bool:
        for sel in ["#consent_wall_optin", "button#consent_wall_optin",
                    "button:has-text('Bestätigen')"]:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    print(f"[BOOKING] Cookie consent via: {sel}")
                    await btn.click()
                    await self._wait_for_loading(timeout_seconds=10)
                    return True
            except Exception:
                continue
        return False

    async def _wait_for_booking_modal(self, timeout_seconds: int = 10) -> bool:
        try:
            await self.page.wait_for_selector(
                MODAL_SELECTOR,
                state="visible",
                timeout=timeout_seconds * 1000,
            )
            self._log("[BOOKING] ✅ Booking modal is visible.")
            return True
        except Exception:
            self._log("[BOOKING] Modal did not become visible before timeout.")
            return False

    async def _activate_from_modal(self) -> bool:
        modal_visible = await self._wait_for_booking_modal(timeout_seconds=10)
        if not modal_visible:
            return False

        clicked = await self.captcha.click_aktivieren()
        if clicked:
            self._log("[BOOKING] ✅ Aktivieren clicked via modal locator.")
        else:
            self._log("[BOOKING] ⚠️ Modal visible but Aktivieren locator did not click.")
        return clicked

    async def _activate_from_html(self, html: str) -> bool:
        """
        Parse the getChangeServiceInfo AJAX response to find the Aktivieren
        button's sendPostAndReplaceContent(url, formId) call, then execute it.
        Bypasses shadow DOM entirely — no DOM search needed.
        """
        m = re.search(
            r"sendPostAndReplaceContent\s*\(\s*['\"]([^'\"]+)['\"],\s*['\"]([^'\"]+)['\"]",
            html,
        )
        if m:
            url     = m.group(1)
            form_id = m.group(2)
            self._log(f"[BOOKING] ✅ Parsed activation: url=`{url}` formId=`{form_id}`")
        else:
            # No explicit URL found — use fallback URL with page's form.
            self._log(
                f"[BOOKING] ⚠️ Could not parse activation URL from response — "
                f"using fallback `{FALLBACK_ACTIVATE_URL}`."
            )
            url     = FALLBACK_ACTIVATE_URL
            form_id = None

        result = await self.page.evaluate(
            """
            ([url, formId]) => {
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
              // Fallback: set form action and submit.
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
            [url, form_id],
        )

        if result in ("sendPost", "form-submit"):
            self._log(f"[BOOKING] ✅ Activation triggered ({result}).")
            await self._wait_for_loading()
            return True

        self._log(f"[BOOKING] ❌ Activation JS returned: `{result}`")
        return False

    async def _activate_directly(self) -> bool:
        """
        Last resort: call sendPostAndReplaceContent('/mytariff/invoice/changeService', ...)
        directly using the form already on the page (contains CSRF token + service code).
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
            FALLBACK_ACTIVATE_URL,
        )
        if result in ("sendPost-direct", "form-submit-direct"):
            self._log(f"[BOOKING] ✅ Direct activation triggered ({result}).")
            await self._wait_for_loading()
            return True
        self._log(f"[BOOKING] ❌ Direct activation returned: `{result}`")
        return False

    async def _wait_for_loading(self, timeout_seconds: int = 30) -> None:
                timeout_ms = timeout_seconds * 1000
                self._log("[BOOKING] ⏳ Waiting for loading indicators to clear...")

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

    async def _verify_success(self) -> bool:
        # Wait for any loading spinner to clear before inspecting page content.
        await self._wait_for_loading()

        # Poll for up to 15 s so the success/failure dialog has time to render
        # (it often arrives a moment after the spinner disappears).
        POLL_INTERVAL = 1.0   # seconds between checks
        POLL_TIMEOUT  = 15.0  # total seconds before giving up
        deadline = asyncio.get_event_loop().time() + POLL_TIMEOUT

        while True:
            try:
                current_url   = self.page.url
                content_lower = (await self.page.content()).lower()
                self._log(f"[BOOKING] Verifying at URL: `{current_url}`")

                # ── Success signals ────────────────────────────────────────
                if await self._has_processing_success_dialog(content_lower):
                    self._log("[BOOKING] ✅ Success modal detected: order is being processed.")
                    return True

                for kw in ["erfolgreich", "gebucht", "buchung bestaetigt",
                           "buchung bestätigt", "bestellung erfolgreich", "successfully"]:
                    if kw in content_lower:
                        self._log(f"[BOOKING] ✅ Success keyword: `{kw}`")
                        return True

                if "success" in current_url.lower() or "bestaetigung" in current_url.lower():
                    self._log("[BOOKING] ✅ Success URL detected.")
                    return True

                # ── Definitive failure signals — stop polling immediately ──
                for kw in ["fehlgeschlagen", "nicht moeglich", "nicht möglich",
                           "ungueltig", "ungültig", "ein fehler ist aufgetreten"]:
                    if kw in content_lower:
                        self._log(f"[BOOKING] ❌ Failure keyword: `{kw}`")
                        return False

            except Exception as e:
                self._log(f"[BOOKING] ❌ Verification error: {type(e).__name__}: {e}")

            if asyncio.get_event_loop().time() >= deadline:
                break

            self._log(f"[BOOKING] ⏳ Outcome not yet clear — polling again...")
            await asyncio.sleep(POLL_INTERVAL)

        self._log("[BOOKING] ❌ Outcome unclear after polling.")
        return False

    async def _has_processing_success_dialog(self, content_lower: str) -> bool:
        # Fast path: the success phrase is unique — no other page element contains it.
        if "dein auftrag ist in bearbeitung" in content_lower:
            return True

        # Robust path: search ANY visible dialog/modal/overlay in the DOM
        # for the success message, independent of element id or class name.
        try:
            return await self.page.evaluate(
                """
                () => {
                  const SUCCESS_TEXT = 'dein auftrag ist in bearbeitung';
                  const HEADLINE_TEXT = 'information';

                  // Helper: is an element actually visible to the user?
                  function isVisible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 && r.height === 0) return false;
                    const s = window.getComputedStyle(el);
                    return s.display !== 'none'
                        && s.visibility !== 'hidden'
                        && s.opacity !== '0';
                  }

                  // 1. Check the known captcha/booking overlay dialog.
                  const knownDialog = document.querySelector('dialog#c-overlay[open]');
                  if (knownDialog && isVisible(knownDialog)) {
                    const text = knownDialog.textContent.toLowerCase();
                    if (text.includes(SUCCESS_TEXT)) return true;
                  }

                  // 2. Check ALL open <dialog> elements.
                  for (const dlg of document.querySelectorAll('dialog[open]')) {
                    if (!isVisible(dlg)) continue;
                    const text = dlg.textContent.toLowerCase();
                    if (text.includes(SUCCESS_TEXT)) return true;
                  }

                  // 3. Check common modal/overlay containers (non-dialog elements).
                  const modalSelectors = [
                    '[role="dialog"]',
                    '[role="alertdialog"]',
                    '.modal',
                    '.overlay',
                    '[class*="c-overlay"]',
                    '[class*="modal"]',
                  ];
                  for (const sel of modalSelectors) {
                    for (const el of document.querySelectorAll(sel)) {
                      if (!isVisible(el)) continue;
                      const text = el.textContent.toLowerCase();
                      if (text.includes(SUCCESS_TEXT)) return true;
                    }
                  }

                  // 4. Last resort: any visible element with both the headline
                  //    and success text, accompanied by a 'Schließen' button.
                  const allEls = document.querySelectorAll('div, section, aside, article');
                  for (const el of allEls) {
                    if (!isVisible(el)) continue;
                    const text = el.textContent.toLowerCase();
                    if (text.includes(HEADLINE_TEXT)
                        && text.includes(SUCCESS_TEXT)
                        && text.includes('schließen')) {
                      return true;
                    }
                  }

                  return false;
                }
                """
            )
        except Exception as e:
            self._log(f"[BOOKING] Dialog-check skipped ({type(e).__name__}).")
            return False

