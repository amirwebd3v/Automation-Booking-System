from unittest.mock import AsyncMock

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


class FakePage:
    def __init__(self, locators=None, text_locator=None, content_html=""):
        self.locators = locators or {}
        self.text_locator = text_locator or FakeLocator(visible=False, count_value=0)
        self.content_html = content_html
        self.wait_for_selector_calls = []
        self.load_state_calls = []
        self.wait_for_function_calls = []

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

    result = await captcha_module.solve_captcha_with_gemini(
        page,
        captcha_module.CAPTCHA_SELECTORS[0],
    )

    assert result == "A1B2"
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
async def test_solve_with_retry_reloads_and_succeeds(monkeypatch):
    handler = captcha_module.CaptchaHandler(page=object())
    solve = AsyncMock(side_effect=["WRONG", "RIGHT"])
    click_aktivieren = AsyncMock(return_value=True)
    is_captcha_error = AsyncMock(side_effect=[True, False])
    reload_captcha_image = AsyncMock(return_value=True)

    monkeypatch.setattr(handler, "solve", solve)
    monkeypatch.setattr(handler, "click_aktivieren", click_aktivieren)
    monkeypatch.setattr(handler, "is_captcha_error", is_captcha_error)
    monkeypatch.setattr(handler, "reload_captcha_image", reload_captcha_image)

    assert await handler.solve_with_retry(max_attempts=3) is True
    assert solve.await_count == 2
    reload_captcha_image.assert_awaited_once()


@pytest.mark.asyncio
async def test_solve_with_retry_raises_after_retry_budget(monkeypatch):
    handler = captcha_module.CaptchaHandler(page=object())
    monkeypatch.setattr(handler, "solve", AsyncMock(return_value="WRONG"))
    monkeypatch.setattr(handler, "click_aktivieren", AsyncMock(return_value=True))
    monkeypatch.setattr(handler, "is_captcha_error", AsyncMock(return_value=True))
    reload_captcha_image = AsyncMock(return_value=True)
    monkeypatch.setattr(handler, "reload_captcha_image", reload_captcha_image)

    with pytest.raises(captcha_module.CaptchaSolveError):
        await handler.solve_with_retry(max_attempts=3)

    assert reload_captcha_image.await_count == 2


@pytest.mark.asyncio
async def test_solve_with_retry_falls_back_to_manual_when_gemini_errors(monkeypatch):
    page = object()
    handler = captcha_module.CaptchaHandler(
        page=page,
        telegram=object(),
        config_manager=object(),
    )
    monkeypatch.setattr(handler, "solve", AsyncMock(side_effect=RuntimeError("quota exceeded")))
    monkeypatch.setattr(
        handler,
        "_request_manual_solution",
        AsyncMock(return_value="AB12C"),
    )
    monkeypatch.setattr(handler, "click_aktivieren", AsyncMock(return_value=True))
    monkeypatch.setattr(handler, "is_captcha_error", AsyncMock(return_value=False))
    fill_input = AsyncMock(return_value=True)
    monkeypatch.setattr(captcha_module, "_fill_captcha_input", fill_input)

    assert await handler.solve_with_retry(max_attempts=3) is True
    fill_input.assert_awaited_once_with(page, "AB12C")