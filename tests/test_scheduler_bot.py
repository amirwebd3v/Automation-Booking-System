"""Tests for scheduler_bot/bot.py."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _bot_env(monkeypatch):
    """Inject the minimum environment variables required to instantiate Sim24Bot."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID",   "999")
    monkeypatch.setenv("GIST_TOKEN",         "gist-tok")
    monkeypatch.setenv("GIST_ID",            "gist-id")
    monkeypatch.setenv("GITHUB_GIST_TOKEN",   "gist-tok")
    monkeypatch.setenv("GITHUB_GIST_ID",      "gist-id")


@pytest.fixture()
def bot():
    from scheduler_bot.bot import Sim24Bot
    return Sim24Bot()


# ── _format_status ────────────────────────────────────────────────────────────

def test_format_status_never_run():
    from scheduler_bot.bot import Sim24Bot
    text = Sim24Bot._format_status(
        {"last_run_ts": 0},
        now=datetime(2024, 1, 1, 12, 34, tzinfo=timezone.utc),
    )
    assert "Never" in text
    assert "26 min" in text
    assert "Last Run Result" in text
    assert "Current State" in text
    assert "Used Data" in text
    assert "Total Data" in text


def test_format_status_with_timestamp():
    from scheduler_bot.bot import Sim24Bot
    text = Sim24Bot._format_status(
        {
            "last_run_ts": 1_700_000_000.0,
            "last_used_kb": int(128.28 * 1024 * 1024),
            "last_total_kb": 130 * 1024 * 1024,
        },
        now=datetime(2024, 1, 1, 12, 34, tzinfo=timezone.utc),
    )
    assert "2023-11-14 23:13" in text
    assert "26 min" in text
    assert "128.28 GB" in text
    assert "130.00 GB" in text


def test_format_status_without_saved_data_explains_missing_snapshot():
    from scheduler_bot.bot import Sim24Bot
    text = Sim24Bot._format_status(
        {"last_run_ts": 0},
        now=datetime(2024, 1, 1, 12, 34, tzinfo=timezone.utc),
    )
    assert "Not yet recorded" in text


def test_format_status_includes_monitoring_mode():
    from scheduler_bot.bot import Sim24Bot
    text = Sim24Bot._format_status(
        {
            "last_run_ts": 0,
            "monitoring_active": False,
        },
        now=datetime(2024, 1, 1, 12, 34, tzinfo=timezone.utc),
    )
    assert "Paused - hourly checks are suspended" in text


def test_format_monitoring_status_auto_mode_changes_with_remaining_data():
    from scheduler_bot.bot import Sim24Bot
    active_text = Sim24Bot._format_monitoring_status(
        {
            "monitoring_active": None,
            "last_used_kb": 49 * 1024 * 1024,
            "last_total_kb": 50 * 1024 * 1024,
        }
    )
    paused_text = Sim24Bot._format_monitoring_status(
        {
            "monitoring_active": None,
            "last_used_kb": 40 * 1024 * 1024,
            "last_total_kb": 50 * 1024 * 1024,
        }
    )
    assert "Auto active" in active_text
    assert "Auto idle" in paused_text


def test_format_status_failed_run_shows_error_and_reason():
    from scheduler_bot.bot import Sim24Bot
    text = Sim24Bot._format_status(
        {
            "last_run_ts": 1_700_000_000.0,
            "last_run_ok": False,
            "last_run_error": "Login failed. Site unavailable.",
        },
        now=datetime(2024, 1, 1, 12, 34, tzinfo=timezone.utc),
    )
    assert "Failed - Login failed. Site unavailable." in text


# ── _on_start ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_start_sends_welcome_with_reply_keyboard(bot):
    from scheduler_bot.bot import _REPLY_KEYBOARD
    bot._send = AsyncMock(return_value=1)
    await bot._on_start()
    bot._send.assert_awaited_once()
    args, kwargs = bot._send.call_args
    assert "Activate" in args[0]
    assert "Pause" in args[0]
    assert kwargs["reply_markup"] == _REPLY_KEYBOARD


# ── _on_status ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_status_sends_gist_data_with_inline_keyboard(bot):
    from scheduler_bot.bot import _STATUS_INLINE
    state = {"last_run_ts": 0, "captcha_pending": False}
    bot._read_gist = AsyncMock(return_value=state)
    bot._send      = AsyncMock(return_value=2)

    await bot._on_status()

    bot._send.assert_awaited_once()
    text, kwargs = bot._send.call_args[0][0], bot._send.call_args[1]
    assert "Never" in text
    assert kwargs["reply_markup"] == _STATUS_INLINE


@pytest.mark.asyncio
async def test_on_status_sends_error_when_gist_unavailable(bot):
    bot._read_gist = AsyncMock(return_value=None)
    bot._send      = AsyncMock(return_value=3)

    await bot._on_status()

    sent_text = bot._send.call_args[0][0]
    assert "❌" in sent_text


# ── _on_book ──────────────────────────────────────────────────────────────────

class _FakeDispatchResponse:
    def __init__(self, status: int, body: str = "", json_payload=None):
        self.status = status
        self._body = body
        self._json_payload = json_payload or {"ok": True, "result": True}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return self._body

    async def json(self):
        return self._json_payload


class _FakeDispatchSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )
        return self.response


@pytest.mark.asyncio
async def test_trigger_workflow_posts_expected_dispatch_request(bot):
    response = _FakeDispatchResponse(status=204)
    session = _FakeDispatchSession(response)
    bot._session = session

    assert await bot._trigger_workflow() is True

    assert len(session.calls) == 1
    request = session.calls[0]
    assert request["url"].endswith("/actions/workflows/check_data.yml/dispatches")
    assert request["headers"]["Authorization"] == "Bearer gist-tok"
    assert request["headers"]["Accept"] == "application/vnd.github+json"
    assert request["headers"]["X-GitHub-Api-Version"] == "2022-11-28"
    assert request["headers"]["User-Agent"] == "amirwebd3v/Automation-Booking-System-py-bot"
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["json"] == {"ref": "main"}
    assert request["timeout"] is not None


@pytest.mark.asyncio
async def test_publish_bot_commands_posts_expected_command_menu(bot):
    response = _FakeDispatchResponse(status=200, json_payload={"ok": True, "result": True})
    session = _FakeDispatchSession(response)
    bot._session = session

    assert await bot._publish_bot_commands() is True

    assert len(session.calls) == 1
    request = session.calls[0]
    assert request["url"].endswith("/setMyCommands")
    assert request["json"] == {
        "commands": [
            {"command": "start", "description": "Open the control menu"},
            {"command": "status", "description": "Show the current monitoring state"},
            {"command": "book", "description": "Trigger a booking now"},
            {"command": "activate", "description": "Force monitoring on"},
            {"command": "pause", "description": "Pause monitoring"},
        ]
    }

@pytest.mark.asyncio
async def test_on_book_sends_success_when_workflow_triggered(bot):
    bot._trigger_workflow = AsyncMock(return_value=True)
    bot._send             = AsyncMock(return_value=4)

    await bot._on_book()

    messages = [call[0][0] for call in bot._send.call_args_list]
    assert any("⏳" in m for m in messages)
    assert any("✅" in m and "Workflow triggered" in m for m in messages)


@pytest.mark.asyncio
async def test_on_book_sends_failure_when_dispatch_fails(bot):
    bot._trigger_workflow = AsyncMock(return_value=False)
    bot._send             = AsyncMock(return_value=5)

    await bot._on_book()

    messages = [call[0][0] for call in bot._send.call_args_list]
    assert any("❌" in m for m in messages)


@pytest.mark.asyncio
async def test_on_activate_sets_monitoring_active_and_confirms(bot):
    state = {"monitoring_active": None}
    bot._read_gist = AsyncMock(return_value=state)
    bot._write_gist = AsyncMock(return_value=True)
    bot._send = AsyncMock(return_value=6)

    await bot._on_activate()

    written_state = bot._write_gist.call_args[0][0]
    assert written_state["monitoring_active"] is True
    assert "activated" in bot._send.call_args[0][0]


@pytest.mark.asyncio
async def test_on_pause_sets_monitoring_active_false_and_confirms(bot):
    state = {"monitoring_active": None}
    bot._read_gist = AsyncMock(return_value=state)
    bot._write_gist = AsyncMock(return_value=True)
    bot._send = AsyncMock(return_value=7)

    await bot._on_pause()

    written_state = bot._write_gist.call_args[0][0]
    assert written_state["monitoring_active"] is False
    assert "paused" in bot._send.call_args[0][0].lower()


# ── _on_captcha_reply ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_captcha_reply_saves_when_pending(bot):
    state = {"last_run_ts": 0, "captcha_pending": True}
    bot._read_gist  = AsyncMock(return_value=state)
    bot._write_gist = AsyncMock(return_value=True)
    bot._send       = AsyncMock(return_value=6)

    await bot._on_captcha_reply("AB12")

    written_state = bot._write_gist.call_args[0][0]
    assert written_state["captcha_reply"]   == "AB12"
    assert written_state["captcha_pending"] is False
    assert "AB12" in bot._send.call_args[0][0]


@pytest.mark.asyncio
async def test_on_captcha_reply_ignored_when_not_pending(bot):
    bot._read_gist  = AsyncMock(return_value={"last_run_ts": 0, "captcha_pending": False})
    bot._write_gist = AsyncMock()
    bot._send       = AsyncMock()

    await bot._on_captcha_reply("XY99")

    bot._write_gist.assert_not_awaited()
    bot._send.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_captcha_reply_reports_write_failure(bot):
    bot._read_gist  = AsyncMock(return_value={"captcha_pending": True})
    bot._write_gist = AsyncMock(return_value=False)
    bot._send       = AsyncMock(return_value=7)

    await bot._on_captcha_reply("ZZ99")

    assert "❌" in bot._send.call_args[0][0]


# ── _on_callback ──────────────────────────────────────────────────────────────

def _make_cq(data: str, msg_id: int = 42) -> dict:
    return {
        "id": "cb-id",
        "data": data,
        "message": {
            "message_id": msg_id,
            "chat": {"id": 999},
        },
    }


@pytest.mark.asyncio
async def test_callback_refresh_edits_status_in_place(bot):
    from scheduler_bot.bot import _STATUS_INLINE
    state = {"last_run_ts": 0, "captcha_pending": False}
    bot._read_gist  = AsyncMock(return_value=state)
    bot._answer_cb  = AsyncMock()
    bot._edit       = AsyncMock()
    bot._send       = AsyncMock()

    await bot._on_callback(_make_cq("refresh", msg_id=77))

    bot._answer_cb.assert_awaited_once_with("cb-id", "Refreshing…")
    bot._edit.assert_awaited_once()
    edit_args = bot._edit.call_args
    assert edit_args[0][0] == 77            # correct message_id
    assert edit_args[0][2] == _STATUS_INLINE
    bot._send.assert_not_awaited()          # no new message, edit only


@pytest.mark.asyncio
async def test_callback_refresh_sends_error_when_gist_fails(bot):
    bot._read_gist  = AsyncMock(return_value=None)
    bot._answer_cb  = AsyncMock()
    bot._edit       = AsyncMock()
    bot._send       = AsyncMock(return_value=8)

    await bot._on_callback(_make_cq("refresh"))

    bot._edit.assert_not_awaited()
    assert "❌" in bot._send.call_args[0][0]


@pytest.mark.asyncio
async def test_callback_book_triggers_workflow_and_reports(bot):
    bot._answer_cb        = AsyncMock()
    bot._trigger_workflow = AsyncMock(return_value=True)
    bot._send             = AsyncMock(return_value=9)

    await bot._on_callback(_make_cq("book"))

    bot._answer_cb.assert_awaited_once_with("cb-id", "Triggering…")
    bot._trigger_workflow.assert_awaited_once()
    assert "✅" in bot._send.call_args[0][0]


# ── _handle_update (auth + dispatch) ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_update_rejects_unauthorized_chat(bot):
    bot._on_start  = AsyncMock()
    bot._on_status = AsyncMock()
    bot._on_book   = AsyncMock()

    update = {"message": {"chat": {"id": 1234}, "text": "/start"}}
    await bot._handle_update(update)

    bot._on_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_update_dispatches_start(bot):
    bot._on_start = AsyncMock()
    await bot._handle_update({"message": {"chat": {"id": 999}, "text": "/start"}})
    bot._on_start.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_update_dispatches_status_button(bot):
    from scheduler_bot.bot import BTN_STATUS
    bot._on_status = AsyncMock()
    await bot._handle_update({"message": {"chat": {"id": 999}, "text": BTN_STATUS}})
    bot._on_status.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_update_dispatches_book_button(bot):
    from scheduler_bot.bot import BTN_BOOK
    bot._on_book = AsyncMock()
    await bot._handle_update({"message": {"chat": {"id": 999}, "text": BTN_BOOK}})
    bot._on_book.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_update_plain_text_goes_to_captcha_reply(bot):
    bot._on_captcha_reply = AsyncMock()
    await bot._handle_update({"message": {"chat": {"id": 999}, "text": "AB12"}})
    bot._on_captcha_reply.assert_awaited_once_with("AB12")


@pytest.mark.asyncio
async def test_handle_update_unknown_command_is_ignored(bot):
    bot._on_captcha_reply = AsyncMock()
    bot._on_start         = AsyncMock()
    await bot._handle_update({"message": {"chat": {"id": 999}, "text": "/unknown"}})
    bot._on_captcha_reply.assert_not_awaited()
    bot._on_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_update_dispatches_callback_query(bot):
    bot._on_callback = AsyncMock()
    update = {
        "callback_query": {
            "id":      "cq1",
            "data":    "refresh",
            "message": {"message_id": 5, "chat": {"id": 999}},
        }
    }
    await bot._handle_update(update)
    bot._on_callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_update_rejects_callback_from_unauthorized_chat(bot):
    bot._on_callback = AsyncMock()
    update = {
        "callback_query": {
            "id":      "cq2",
            "data":    "book",
            "message": {"message_id": 5, "chat": {"id": 1234}},
        }
    }
    await bot._handle_update(update)
    bot._on_callback.assert_not_awaited()


# ── env var fallback ──────────────────────────────────────────────────────────

def test_env_fallback_uses_gist_token_when_github_gist_token_absent(monkeypatch):
    monkeypatch.delenv("GITHUB_GIST_TOKEN", raising=False)
    monkeypatch.setenv("GIST_TOKEN", "fallback-tok")
    from scheduler_bot import bot as bot_module
    import importlib
    importlib.reload(bot_module)
    b = bot_module.Sim24Bot()
    assert b.gist_token == "fallback-tok"


def test_env_github_gist_token_takes_precedence(monkeypatch):
    monkeypatch.setenv("GITHUB_GIST_TOKEN", "primary-tok")
    monkeypatch.setenv("GIST_TOKEN",        "secondary-tok")
    from scheduler_bot import bot as bot_module
    import importlib
    importlib.reload(bot_module)
    b = bot_module.Sim24Bot()
    assert b.gist_token == "primary-tok"
