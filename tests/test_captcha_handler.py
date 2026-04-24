from unittest.mock import AsyncMock, MagicMock

import pytest

import captcha_handler as captcha_module


class FakeLocator:
    def __init__(
        self,
        *,
        visible=True,
        count_value=1,
        screenshot_bytes=b"captcha-image",
        attributes=None,
        role_children=None,
        selector_children=None,
    ):
        self.visible = visible
        self.count_value = count_value
        self.screenshot_bytes = screenshot_bytes
        self.attributes = attributes or {}
        self.role_children = role_children or {}
        self.selector_children = selector_children or {}
        self.filled = None
        self.clicked = False

    @property
    def first(self):
        return self

    async def wait_for(self, state=None, timeout=None):
        return None

    async def screenshot(self, type="png"):
        return self.screenshot_bytes

    async def fill(self, value):
        self.filled = value

    async def count(self):
        return self.count_value

    async def is_visible(self):
        return self.visible

    async def click(self):
        self.clicked = True

    async def get_attribute(self, name):
        return self.attributes.get(name)

    def get_by_role(self, role, name=None):
        return self.role_children.get((role, name), FakeLocator(visible=False, count_value=0))

    def locator(self, selector):
        return self.selector_children.get(selector, FakeLocator(visible=False, count_value=0))


class FakeResponse:
    def __init__(self, body=b"", ok=True):
        self._body = body
        self.ok = ok

    async def body(self):
        return self._body


class FakeAPIRequestContext:
    """Simulates page.request — returns empty/failed by default so tests fall back to screenshot."""

    def __init__(self, ok=False):
        self._ok = ok

    async def get(self, url):
        return FakeResponse(body=b"", ok=self._ok)


class FakePage:
    def __init__(self, locators=None, text_locator=None, content_html="", request_ok=False):
        self.locators = locators or {}
        self.text_locator = text_locator or FakeLocator(visible=False, count_value=0)
        self.content_html = content_html
        self.wait_for_selector_calls = []
        self.load_state_calls = []
        self.wait_for_function_calls = []
        self.request = FakeAPIRequestContext(ok=request_ok)

    def locator(self, selector):
        return self.locators.get(selector, FakeLocator(visible=False, count_value=0))

    def get_by_text(self, text):
        return self.text_locator

    async def wait_for_selector(self, selector, state=None, timeout=None):
        self.wait_for_selector_calls.append((selector, state, timeout))

    async def wait_for_load_state(self, state, timeout=None):
        self.load_state_calls.append((state, timeout))

    async def wait_for_function(self, script, args, timeout=None):
        self.wait_for_function_calls.append((script, args, timeout))

    async def content(self):
        return self.content_html

    async def evaluate(self, expression, arg=None):
        # Return a plausible absolute URL for any relative src passed in.
        return f"https://example.com/{arg}" if arg else "https://example.com/"


@pytest.mark.asyncio
async def test_solve_captcha_with_gemini_screenshots_and_fills_input(monkeypatch):
    captcha_locator = FakeLocator(screenshot_bytes=b"captcha")
    input_locator = FakeLocator()
    page = FakePage(
        {
            captcha_module.CAPTCHA_SELECTORS[0]: captcha_locator,
            captcha_module.CAPTCHA_INPUT_SELECTORS[0]: input_locator,
        }
    )

    async def fake_extract(image_b64):
        assert image_b64 == "Y2FwdGNoYQ=="
        return "A1B2"

    monkeypatch.setattr(captcha_module, "_extract_gemini_text", fake_extract)

    solution, image_bytes = await captcha_module.solve_captcha_with_gemini(
        page,
        captcha_module.CAPTCHA_SELECTORS[0],
    )

    assert solution == "A1B2"
    assert image_bytes == b"captcha"
    assert input_locator.filled == "A1B2"


@pytest.mark.asyncio
async def test_click_aktivieren_uses_modal_text_locators(monkeypatch):
    activate_link = FakeLocator()
    modal = FakeLocator(
        role_children={("link", "Aktivieren"): activate_link},
    )
    page = FakePage({captcha_module.MODAL_SELECTOR: modal})
    handler = captcha_module.CaptchaHandler(page)
    handler.wait_for_loading = AsyncMock()

    assert await handler.click_aktivieren() is True
    assert activate_link.clicked is True
    handler.wait_for_loading.assert_awaited_once()


@pytest.mark.asyncio
async def test_solve_with_retry_gemini_succeeds_on_second_attempt(monkeypatch):
    """Gemini gets the answer wrong on attempt 1, right on attempt 2."""
    handler = captcha_module.CaptchaHandler(page=object())

    _solve_with_screenshot = AsyncMock(return_value=("SOLUTION", b"img"))
    _notify_gemini_answer = AsyncMock()
    click_aktivieren = AsyncMock(return_value=True)
    is_captcha_error = AsyncMock(side_effect=[True, False])
    reload_captcha_image = AsyncMock(return_value=True)

    monkeypatch.setattr(handler, "_solve_with_screenshot", _solve_with_screenshot)
    monkeypatch.setattr(handler, "_notify_gemini_answer", _notify_gemini_answer)
    monkeypatch.setattr(handler, "click_aktivieren", click_aktivieren)
    monkeypatch.setattr(handler, "is_captcha_error", is_captcha_error)
    monkeypatch.setattr(handler, "reload_captcha_image", reload_captcha_image)

    assert await handler.solve_with_retry(max_attempts=2) is True
    assert _solve_with_screenshot.await_count == 2
    reload_captcha_image.assert_awaited_once()


@pytest.mark.asyncio
async def test_solve_with_retry_falls_back_to_manual_when_gemini_exhausted(monkeypatch):
    """Gemini always returns wrong answer — manual loop must be reached."""
    handler = captcha_module.CaptchaHandler(page=object(), telegram=object())

    monkeypatch.setattr(handler, "_solve_with_screenshot", AsyncMock(return_value=("X", b"img")))
    monkeypatch.setattr(handler, "_notify_gemini_answer", AsyncMock())
    monkeypatch.setattr(handler, "click_aktivieren", AsyncMock(return_value=True))
    monkeypatch.setattr(handler, "is_captcha_error", AsyncMock(return_value=True))
    monkeypatch.setattr(handler, "reload_captcha_image", AsyncMock(return_value=True))

    solve_manually = AsyncMock()
    monkeypatch.setattr(handler, "_solve_manually_until_accepted", solve_manually)

    assert await handler.solve_with_retry(max_attempts=2) is True
    solve_manually.assert_awaited_once()


@pytest.mark.asyncio
async def test_solve_with_retry_falls_back_to_manual_when_gemini_errors(monkeypatch):
    """Any exception from Gemini must immediately fall through to manual."""
    handler = captcha_module.CaptchaHandler(page=object(), telegram=object())

    monkeypatch.setattr(
        handler,
        "_solve_with_screenshot",
        AsyncMock(side_effect=RuntimeError("quota exceeded")),
    )
    monkeypatch.setattr(handler, "reload_captcha_image", AsyncMock(return_value=True))

    solve_manually = AsyncMock()
    monkeypatch.setattr(handler, "_solve_manually_until_accepted", solve_manually)

    assert await handler.solve_with_retry(max_attempts=2) is True
    # Gemini broke on attempt 1 — manual must have been called exactly once.
    solve_manually.assert_awaited_once()


@pytest.mark.asyncio
async def test_solve_with_retry_raises_without_telegram_when_gemini_fails(monkeypatch):
    """Without Telegram, failing Gemini must raise CaptchaSolveError."""
    handler = captcha_module.CaptchaHandler(page=object(), telegram=None)

    monkeypatch.setattr(
        handler,
        "_solve_with_screenshot",
        AsyncMock(side_effect=RuntimeError("no quota")),
    )
    monkeypatch.setattr(handler, "reload_captcha_image", AsyncMock(return_value=True))

    with pytest.raises(captcha_module.CaptchaSolveError):
        await handler.solve_with_retry(max_attempts=2)