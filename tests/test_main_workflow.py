import sys
from pathlib import Path

import pytest

# Ensure tests can import root modules in CI/pytest importlib mode.
ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import main as workflow_main


class FakePage:
    pass


class FakeBrowser:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_full_workflow_books_when_below_threshold(monkeypatch):
    state = {
        "updated": False,
        "booking_called": False,
        "browser": None,
        "messages": [],
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
            state["updated"] = True

    class FakeTelegram:
        def __init__(self, token, chat_id):
            assert token == "token"
            assert chat_id == "chat"

        async def send(self, text):
            state["messages"].append(text)
            return True

    class FakeLogin:
        def __init__(self, username, password, telegram):
            assert username == "user"
            assert password == "pass"

        async def login(self):
            browser = FakeBrowser()
            state["browser"] = browser
            return browser, FakePage()

    class FakeDataChecker:
        def __init__(self, page):
            assert isinstance(page, FakePage)

        async def get_usage(self):
            total_kb = 56 * 1024 * 1024
            used_kb = int(55.7 * 1024 * 1024)
            return used_kb, total_kb

    class FakeBookingModule:
        def __init__(self, page, telegram):
            assert isinstance(page, FakePage)

        async def book_2gb_packet(self):
            state["booking_called"] = True
            return True

    monkeypatch.setattr(workflow_main, "ConfigManager", FakeConfig)
    monkeypatch.setattr(workflow_main, "TelegramNotifier", FakeTelegram)
    monkeypatch.setattr(workflow_main, "Sim24Login", FakeLogin)
    monkeypatch.setattr(workflow_main, "DataChecker", FakeDataChecker)
    monkeypatch.setattr(workflow_main, "BookingModule", FakeBookingModule)

    await workflow_main.main()

    assert state["booking_called"] is True
    assert state["updated"] is True
    assert state["browser"] is not None and state["browser"].closed is True
    assert any("Data Usage Report" in msg for msg in state["messages"])
    assert any("booked successfully" in msg for msg in state["messages"])


@pytest.mark.asyncio
async def test_full_workflow_skips_booking_when_above_threshold(monkeypatch):
    state = {
        "updated": False,
        "booking_constructed": False,
        "messages": [],
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
            state["updated"] = True

    class FakeTelegram:
        def __init__(self, token, chat_id):
            pass

        async def send(self, text):
            state["messages"].append(text)
            return True

    class FakeLogin:
        async def login(self):
            return FakeBrowser(), FakePage()

        def __init__(self, username, password, telegram):
            pass

    class FakeDataChecker:
        def __init__(self, page):
            pass

        async def get_usage(self):
            total_kb = 56 * 1024 * 1024
            used_kb = int(50.0 * 1024 * 1024)
            return used_kb, total_kb

    class FakeBookingModule:
        def __init__(self, page, telegram):
            state["booking_constructed"] = True

        async def book_2gb_packet(self):
            return True

    monkeypatch.setattr(workflow_main, "ConfigManager", FakeConfig)
    monkeypatch.setattr(workflow_main, "TelegramNotifier", FakeTelegram)
    monkeypatch.setattr(workflow_main, "Sim24Login", FakeLogin)
    monkeypatch.setattr(workflow_main, "DataChecker", FakeDataChecker)
    monkeypatch.setattr(workflow_main, "BookingModule", FakeBookingModule)

    await workflow_main.main()

    assert state["booking_constructed"] is False
    assert state["updated"] is True
    assert any("No action needed" in msg for msg in state["messages"])


@pytest.mark.asyncio
async def test_full_workflow_reports_login_failure(monkeypatch):
    state = {
        "updated": False,
        "messages": [],
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
            state["updated"] = True

    class FakeTelegram:
        def __init__(self, token, chat_id):
            pass

        async def send(self, text):
            state["messages"].append(text)
            return True

    class FakeLogin:
        def __init__(self, username, password, telegram):
            pass

        async def login(self):
            return None, None

    monkeypatch.setattr(workflow_main, "ConfigManager", FakeConfig)
    monkeypatch.setattr(workflow_main, "TelegramNotifier", FakeTelegram)
    monkeypatch.setattr(workflow_main, "Sim24Login", FakeLogin)

    await workflow_main.main()

    assert state["updated"] is True
    assert any("Login failed" in msg for msg in state["messages"])