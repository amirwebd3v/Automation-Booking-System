"""Tests for scheduler_bot/bot.py."""

import json
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
    monkeypatch.setenv("GITHUB_PAT",         "gh-pat")


@pytest.fixture()
def bot():
    from scheduler_bot.bot import Sim24Bot
    return Sim24Bot()


# ── _format_status ────────────────────────────────────────────────────────────

def test_format_status_never_run():
    from scheduler_bot.bot import Sim24Bot
    text = Sim24Bot._format_status({"last_run_ts": 0})
    assert "Never" in text
    assert "✅ None" in text


def test_format_status_with_timestamp():
    from scheduler_bot.bot import Sim24Bot
    text = Sim24Bot._format_status({"last_run_ts": 1_700_000_000.0})
    assert "2023" in text  # timestamp 1_700_000_000 is in 2023
    assert "✅ None" in text


def test_format_status_captcha_pending():
    from scheduler_bot.bot import Sim24Bot
    text = Sim24Bot._format_status({"last_run_ts": 0, "captcha_pending": True})
    assert "Waiting for your reply" in text


# ── _on_start ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_start_sends_welcome_with_reply_keyboard(bot):
    from scheduler_bot.bot import _REPLY_KEYBOARD
    bot._send = AsyncMock(return_value=1)
    await bot._on_start()
    bot._send.assert_awaited_once()
    _, kwargs = bot._send.call_args
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
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    monkeypatch.delenv("GITHUB_GIST_TOKEN", raising=False)
    monkeypatch.setenv("GIST_TOKEN", "fallback-tok")
    from scheduler_bot import bot as bot_module
    import importlib
    importlib.reload(bot_module)
    b = bot_module.Sim24Bot()
    assert b.gist_token == "fallback-tok"
    assert b.gh_pat     == "fallback-tok"


def test_env_github_gist_token_takes_precedence(monkeypatch):
    monkeypatch.setenv("GITHUB_GIST_TOKEN", "primary-tok")
    monkeypatch.setenv("GIST_TOKEN",        "secondary-tok")
    from scheduler_bot import bot as bot_module
    import importlib
    importlib.reload(bot_module)
    b = bot_module.Sim24Bot()
    assert b.gist_token == "primary-tok"
