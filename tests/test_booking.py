from unittest.mock import AsyncMock

import pytest

from booking import BookingModule


class FakeButton:
    def __init__(self, disabled=None, aria_disabled=None):
        self.attributes = {
            "disabled": disabled,
            "aria-disabled": aria_disabled,
        }
        self.click_calls = []

    async def get_attribute(self, name):
        return self.attributes.get(name)

    async def click(self, timeout=None, force=False):
        self.click_calls.append({"timeout": timeout, "force": force})

    async def is_visible(self):
        return True


class FakeResponse:
    def __init__(self, text):
        self.status = 200
        self._text = text

    async def text(self):
        return self._text


class AwaitableValue:
    def __init__(self, value):
        self.value = value

    def __await__(self):
        async def _inner():
            return self.value

        return _inner().__await__()


class FakeResponseInfo:
    def __init__(self, response):
        self.value = AwaitableValue(response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePage:
    def __init__(self, response_text="<div></div>", evaluate_result=None):
        self.url = "https://service.sim24.de/mytariff"
        self.response_text = response_text
        self.evaluate_result = evaluate_result
        self.evaluate_calls = []

    def expect_response(self, predicate, timeout=None):
        self.expect_timeout = timeout
        return FakeResponseInfo(FakeResponse(self.response_text))

    async def evaluate(self, script, arg=None):
        self.evaluate_calls.append((script, arg))
        return self.evaluate_result


@pytest.mark.asyncio
async def test_book_packet_returns_false_when_button_missing(monkeypatch):
    booker = BookingModule(page=FakePage(), telegram=None)
    monkeypatch.setattr(booker, "_find_booking_button", AsyncMock(return_value=(None, None)))

    assert await booker.book_2gb_packet() is False


@pytest.mark.asyncio
async def test_activate_from_html_parses_activation_url_and_waits(monkeypatch):
    page = FakePage(evaluate_result="sendPost")
    booker = BookingModule(page=page, telegram=None)
    wait_for_loading = AsyncMock()
    monkeypatch.setattr(booker, "_wait_for_loading", wait_for_loading)

    html = "<a onclick=\"sendPostAndReplaceContent('/activate','form-1')\"></a>"

    assert await booker._activate_from_html(html) is True
    assert page.evaluate_calls[0][1] == ["/activate", "form-1"]
    wait_for_loading.assert_awaited_once()


@pytest.mark.asyncio
async def test_book_packet_uses_modal_activation_when_available(monkeypatch):
    page = FakePage(response_text="<div>modal</div>")
    button = FakeButton()
    booker = BookingModule(page=page, telegram=None)

    monkeypatch.setattr(booker, "_find_booking_button", AsyncMock(return_value=(button, "selector")))
    monkeypatch.setattr(booker, "_wait_for_booking_modal", AsyncMock(return_value=True))
    monkeypatch.setattr(booker, "_handle_cookie_consent", AsyncMock(return_value=False))
    monkeypatch.setattr(booker.captcha, "is_captcha_present", AsyncMock(return_value=False))
    activate_from_modal = AsyncMock(return_value=True)
    verify_success = AsyncMock(return_value=True)
    monkeypatch.setattr(booker, "_wait_for_loading", AsyncMock())
    monkeypatch.setattr(booker, "_activate_from_modal", activate_from_modal)
    monkeypatch.setattr(booker, "_verify_success", verify_success)

    assert await booker.book_2gb_packet() is True
    assert button.click_calls == [{"timeout": 10_000, "force": False}]
    activate_from_modal.assert_awaited_once()
    verify_success.assert_awaited_once()


@pytest.mark.asyncio
async def test_book_packet_handles_post_activation_captcha(monkeypatch):
    page = FakePage(response_text="<div>modal</div>")
    button = FakeButton()
    booker = BookingModule(page=page, telegram=None)

    monkeypatch.setattr(booker, "_find_booking_button", AsyncMock(return_value=(button, "selector")))
    monkeypatch.setattr(booker, "_wait_for_booking_modal", AsyncMock(return_value=True))
    monkeypatch.setattr(booker, "_handle_cookie_consent", AsyncMock(return_value=False))
    monkeypatch.setattr(booker, "_activate_from_modal", AsyncMock(return_value=False))
    monkeypatch.setattr(booker, "_activate_from_html", AsyncMock(return_value=False))
    monkeypatch.setattr(booker, "_activate_directly", AsyncMock(return_value=True))
    monkeypatch.setattr(booker, "_wait_for_loading", AsyncMock())
    monkeypatch.setattr(booker, "_verify_success", AsyncMock(return_value=True))
    monkeypatch.setattr(booker.captcha, "is_captcha_present", AsyncMock(side_effect=[False, True]))
    solve_with_retry = AsyncMock(return_value=True)
    monkeypatch.setattr(booker.captcha, "solve_with_retry", solve_with_retry)

    assert await booker.book_2gb_packet() is True
    solve_with_retry.assert_awaited_once_with(max_attempts=3)