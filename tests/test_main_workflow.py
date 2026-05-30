import sys
from pathlib import Path

import pytest

# Ensure tests can import root modules in CI/pytest importlib mode.
ROOT_DIR = Path(__file__).resolve().parents[1]
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
        "recorded": None,
        "snapshot": None,
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

        def record_run(self, *, success, error="", used_kb=None, total_kb=None):
            state["updated"] = True
            state["recorded"] = {
                "success": success,
                "error": error,
                "used_kb": used_kb,
                "total_kb": total_kb,
            }

        def record_usage_snapshot(self, *, used_kb, total_kb):
            state["snapshot"] = {
                "used_kb": used_kb,
                "total_kb": total_kb,
            }

    class FakeTelegram:
        def __init__(self, token, chat_id):
            assert token == "token"
            assert chat_id == "chat"

        async def send(self, text):
            state["messages"].append(text)
            return True

        async def send_photo(self, image_bytes, caption=""):
            state["messages"].append(caption)
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
        def __init__(self, page, telegram, config_manager=None):
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
    assert state["recorded"]["success"] is True
    assert state["recorded"]["error"] == ""
    assert state["snapshot"] == {
        "used_kb": state["recorded"]["used_kb"],
        "total_kb": state["recorded"]["total_kb"],
    }
    assert state["browser"] is not None and state["browser"].closed is True
    assert any("Run complete" in msg for msg in state["messages"])
    assert any("2 GB packet booked successfully" in msg for msg in state["messages"])


@pytest.mark.asyncio
async def test_full_workflow_skips_booking_when_above_threshold(monkeypatch):
    state = {
        "updated": False,
        "recorded": None,
        "snapshot": None,
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

        def record_run(self, *, success, error="", used_kb=None, total_kb=None):
            state["updated"] = True
            state["recorded"] = {
                "success": success,
                "error": error,
                "used_kb": used_kb,
                "total_kb": total_kb,
            }

        def record_usage_snapshot(self, *, used_kb, total_kb):
            state["snapshot"] = {
                "used_kb": used_kb,
                "total_kb": total_kb,
            }

    class FakeTelegram:
        def __init__(self, token, chat_id):
            pass

        async def send(self, text):
            state["messages"].append(text)
            return True

        async def send_photo(self, image_bytes, caption=""):
            state["messages"].append(caption)
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
        def __init__(self, page, telegram, config_manager=None):
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
    assert state["recorded"]["success"] is True
    assert state["recorded"]["error"] == ""
    assert state["snapshot"] is not None
    assert any("Run complete" in msg for msg in state["messages"])
    assert any("No action needed" in msg for msg in state["messages"])


@pytest.mark.asyncio
async def test_full_workflow_reports_login_failure(monkeypatch):
    state = {
        "updated": False,
        "recorded": None,
        "snapshot": None,
        "messages": [],
    }

    class FakeConfig:
        def __init__(self):
            self.telegram_token = "token"
            self.telegram_chat_id = "chat"
            self.sim24_username = "user"
            self.sim24_password = "pass"
            self.last_run_ts = 0

        def record_run(self, *, success, error="", used_kb=None, total_kb=None):
            state["updated"] = True
            state["recorded"] = {
                "success": success,
                "error": error,
                "used_kb": used_kb,
                "total_kb": total_kb,
            }

        def record_usage_snapshot(self, *, used_kb, total_kb):
            state["snapshot"] = {
                "used_kb": used_kb,
                "total_kb": total_kb,
            }

    class FakeTelegram:
        def __init__(self, token, chat_id):
            pass

        async def send(self, text):
            state["messages"].append(text)
            return True

        async def send_photo(self, image_bytes, caption=""):
            state["messages"].append(caption)
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
    assert state["recorded"]["success"] is False
    assert "Login failed" in state["recorded"]["error"]
    assert state["snapshot"] is None
    assert any("Login failed" in msg for msg in state["messages"])