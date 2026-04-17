from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import login as login_module
from captcha_handler import CaptchaAutomationError, CaptchaSolveError


class FakeLocator:
    @property
    def first(self):
        return self

    async def count(self):
        return 0

    async def is_visible(self):
        return False


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.goto_calls = []
        self.wait_for_selector_calls = []
        self.fill_calls = []
        self.press_calls = []
        self.load_state_calls = []

    async def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append((url, wait_until, timeout))
        self.url = url

    async def wait_for_selector(self, selector, timeout=None):
        self.wait_for_selector_calls.append((selector, timeout))

    async def fill(self, selector, value):
        self.fill_calls.append((selector, value))

    async def press(self, selector, key):
        self.press_calls.append((selector, key))

    async def query_selector(self, selector):
        return None

    def locator(self, selector):
        return FakeLocator()

    async def wait_for_load_state(self, state, timeout=None):
        self.load_state_calls.append((state, timeout))


class FakeContext:
    def __init__(self):
        self.closed = False
        self.storage_state_calls = []

    async def close(self):
        self.closed = True

    async def storage_state(self, path):
        self.storage_state_calls.append(path)


class FakeBrowser:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def _patch_playwright(monkeypatch, browser):
    fake_playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=AsyncMock(return_value=browser))
    )
    monkeypatch.setattr(
        login_module,
        "async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=fake_playwright)),
    )


@pytest.mark.asyncio
async def test_login_reuses_existing_session(monkeypatch, tmp_path):
    state_path = tmp_path / "storage_state.json"
    state_path.write_text("{}")
    monkeypatch.setattr(login_module, "STORAGE_STATE_PATH", state_path)
    monkeypatch.setattr(login_module.asyncio, "sleep", AsyncMock())

    browser = FakeBrowser()
    context = FakeContext()
    page = FakePage()
    _patch_playwright(monkeypatch, browser)

    create_context = AsyncMock(return_value=context)
    new_page = AsyncMock(return_value=page)
    load_existing_session = AsyncMock(return_value=True)
    monkeypatch.setattr(login_module.Sim24Login, "_create_context", create_context)
    monkeypatch.setattr(login_module.Sim24Login, "_new_stealth_page", new_page)
    monkeypatch.setattr(login_module.Sim24Login, "_load_existing_session", load_existing_session)

    login_runner = login_module.Sim24Login("user", "pass")

    result_browser, result_page = await login_runner.login()

    assert result_browser is browser
    assert result_page is page
    assert page.goto_calls == [(login_module.DATA_URL, "domcontentloaded", 30_000)]
    assert page.fill_calls == []
    assert context.storage_state_calls == []


@pytest.mark.asyncio
async def test_login_falls_back_to_fresh_login_and_saves_storage_state(monkeypatch, tmp_path):
    state_path = tmp_path / "storage_state.json"
    state_path.write_text("{}")
    monkeypatch.setattr(login_module, "STORAGE_STATE_PATH", state_path)
    monkeypatch.setattr(login_module.asyncio, "sleep", AsyncMock())

    browser = FakeBrowser()
    stale_context = FakeContext()
    fresh_context = FakeContext()
    stale_page = FakePage()
    fresh_page = FakePage()
    _patch_playwright(monkeypatch, browser)

    monkeypatch.setattr(
        login_module.Sim24Login,
        "_create_context",
        AsyncMock(side_effect=[stale_context, fresh_context]),
    )
    monkeypatch.setattr(
        login_module.Sim24Login,
        "_new_stealth_page",
        AsyncMock(side_effect=[stale_page, fresh_page]),
    )
    monkeypatch.setattr(
        login_module.Sim24Login,
        "_load_existing_session",
        AsyncMock(return_value=False),
    )

    async def fake_click_submit(self, page):
        page.url = login_module.SUCCESS_URL
        return True

    monkeypatch.setattr(login_module.Sim24Login, "_click_submit", fake_click_submit)

    login_runner = login_module.Sim24Login("user", "pass")

    result_browser, result_page = await login_runner.login()

    assert result_browser is browser
    assert result_page is fresh_page
    assert stale_context.closed is True
    assert fresh_context.storage_state_calls == [str(state_path)]
    assert fresh_page.fill_calls == [
        (login_module.LOGIN_FORM_SELECTOR, "user"),
        ("#UserLoginType_password", "pass"),
    ]
    assert [call[0] for call in fresh_page.goto_calls] == [login_module.LOGIN_URL, login_module.DATA_URL]


@pytest.mark.asyncio
async def test_handle_login_captcha_raises_after_max_attempts():
    login_runner = login_module.Sim24Login("user", "pass")
    captcha = SimpleNamespace(
        solve=AsyncMock(side_effect=CaptchaAutomationError("bad solve")),
        reload_captcha_image=AsyncMock(return_value=True),
    )

    with pytest.raises(CaptchaSolveError):
        await login_runner._handle_login_captcha(captcha)

    assert captcha.solve.await_count == login_module.CAPTCHA_MAX_ATTEMPTS
    assert captcha.reload_captcha_image.await_count == login_module.CAPTCHA_MAX_ATTEMPTS - 1