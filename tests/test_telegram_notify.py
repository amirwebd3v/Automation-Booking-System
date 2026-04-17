import pytest

from telegram_notify import TelegramNotifier


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses, collector, should_raise=False):
        self.responses = list(responses)
        self.collector = collector
        self.should_raise = should_raise

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, json=None, data=None, timeout=None):
        if self.should_raise:
            raise RuntimeError("network down")
        self.collector.append({
            "url": url,
            "json": json,
            "data": data,
            "timeout": timeout,
        })
        return FakeResponse(self.responses.pop(0))


@pytest.mark.asyncio
async def test_send_posts_markdown_message(monkeypatch):
    calls = []

    monkeypatch.setattr(
        "telegram_notify.aiohttp.ClientSession",
        lambda: FakeSession([{ "ok": True }], calls),
    )

    notifier = TelegramNotifier(token="token", chat_id="chat")

    assert await notifier.send("hello") is True
    assert calls[0]["url"].endswith("/sendMessage")
    assert calls[0]["json"] == {
        "chat_id": "chat",
        "text": "hello",
        "parse_mode": "Markdown",
    }


@pytest.mark.asyncio
async def test_send_photo_posts_form_data(monkeypatch):
    calls = []

    monkeypatch.setattr(
        "telegram_notify.aiohttp.ClientSession",
        lambda: FakeSession([{ "ok": True }], calls),
    )

    notifier = TelegramNotifier(token="token", chat_id="chat")

    assert await notifier.send_photo(b"image", caption="caption") is True
    assert calls[0]["url"].endswith("/sendPhoto")
    assert calls[0]["data"] is not None


@pytest.mark.asyncio
async def test_send_returns_false_on_transport_exception(monkeypatch):
    monkeypatch.setattr(
        "telegram_notify.aiohttp.ClientSession",
        lambda: FakeSession([], [], should_raise=True),
    )

    notifier = TelegramNotifier(token="token", chat_id="chat")

    assert await notifier.send("hello") is False