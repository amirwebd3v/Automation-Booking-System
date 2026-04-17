from unittest.mock import AsyncMock

import pytest

import main as workflow_main
from captcha_handler import CaptchaSolveError


class FakeBrowser:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakePage:
    async def screenshot(self, full_page=True):
        return b"shot"


def test_build_run_summary_formats_expected_action_text():
    assert "No action needed" in workflow_main._build_run_summary(9.0, 1.0, 10.0, False, None)
    assert "2 GB packet booked successfully" in workflow_main._build_run_summary(9.9, 0.1, 10.0, True, True)


@pytest.mark.asyncio
async def test_send_error_alert_prefers_photo_when_page_available():
    sent_messages = []
    sent_photos = []

    class FakeTelegram:
        async def send(self, text):
            sent_messages.append(text)
            return True

        async def send_photo(self, image_bytes, caption=""):
            sent_photos.append((image_bytes, caption))
            return True

    await workflow_main._send_error_alert(FakeTelegram(), "error", FakePage())

    assert sent_messages == []
    assert sent_photos == [(b"shot", "error")]


@pytest.mark.asyncio
async def test_main_sends_photo_alert_when_captcha_solver_fails(monkeypatch):
    state = {
        "messages": [],
        "captions": [],
        "browser": None,
    }

    class FakeConfig:
        def __init__(self):
            self.telegram_token = "token"
            self.telegram_chat_id = "chat"
            self.sim24_username = "user"
            self.sim24_password = "pass"
            self.last_run_ts = 0
            self.interval_minutes = 30

        def is_time_to_run(self):
            return True

        def update_last_run(self):
            return None

    class RecordingTelegram:
        def __init__(self, token, chat_id):
            pass

        async def send(self, text):
            state["messages"].append(text)
            return True

        async def send_photo(self, image_bytes, caption=""):
            state["captions"].append(caption)
            return True

    class FakeLogin:
        def __init__(self, username, password, telegram):
            pass

        async def login(self):
            browser = FakeBrowser()
            state["browser"] = browser
            return browser, FakePage()

    class FakeDataChecker:
        def __init__(self, page):
            pass

        async def get_usage(self):
            total_kb = 56 * 1024 * 1024
            used_kb = int(55.8 * 1024 * 1024)
            return used_kb, total_kb

    class FailingBookingModule:
        def __init__(self, page, telegram):
            pass

        async def book_2gb_packet(self):
            raise CaptchaSolveError("solver exhausted")

    monkeypatch.setattr(workflow_main, "ConfigManager", FakeConfig)
    monkeypatch.setattr(workflow_main, "TelegramNotifier", RecordingTelegram)
    monkeypatch.setattr(workflow_main, "Sim24Login", FakeLogin)
    monkeypatch.setattr(workflow_main, "DataChecker", FakeDataChecker)
    monkeypatch.setattr(workflow_main, "BookingModule", FailingBookingModule)

    await workflow_main.main()

    assert state["browser"].closed is True
    assert any("Gemini failed to solve the CAPTCHA" in caption for caption in state["captions"])