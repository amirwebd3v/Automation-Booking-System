import os
import base64
import struct
from pathlib import Path
import zlib

import pytest
from dotenv import load_dotenv

import captcha_handler as captcha_module
import login as login_module
import main as workflow_main
from booking import BookingModule
from data_checker import DataChecker
from login import Sim24Login
from telegram_notify import TelegramNotifier


load_dotenv(Path(__file__).resolve().parents[1] / ".env")


GLYPHS = {
    "A": [
        "01110",
        "10001",
        "10001",
        "11111",
        "10001",
        "10001",
        "10001",
    ],
    "B": [
        "11110",
        "10001",
        "10001",
        "11110",
        "10001",
        "10001",
        "11110",
    ],
    "2": [
        "01110",
        "10001",
        "00001",
        "00010",
        "00100",
        "01000",
        "11111",
    ],
    "4": [
        "00010",
        "00110",
        "01010",
        "10010",
        "11111",
        "00010",
        "00010",
    ],
}


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def _build_test_png(text: str, scale: int = 18, padding: int = 10) -> bytes:
    glyph_width = len(GLYPHS[text[0]][0])
    glyph_height = len(GLYPHS[text[0]])
    spacing = scale
    width = padding * 2 + len(text) * glyph_width * scale + (len(text) - 1) * spacing
    height = padding * 2 + glyph_height * scale

    pixels = [[[255, 255, 255] for _ in range(width)] for _ in range(height)]

    cursor_x = padding
    for char in text:
        glyph = GLYPHS[char]
        for row_index, row in enumerate(glyph):
            for col_index, bit in enumerate(row):
                if bit != "1":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        y = padding + row_index * scale + dy
                        x = cursor_x + col_index * scale + dx
                        pixels[y][x] = [0, 0, 0]
        cursor_x += glyph_width * scale + spacing

    raw_rows = []
    for row in pixels:
        raw_rows.append(b"\x00" + bytes(channel for pixel in row for channel in pixel))

    compressed = zlib.compress(b"".join(raw_rows), level=9)
    header = b"\x89PNG\r\n\x1a\n"
    ihdr = _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    idat = _png_chunk(b"IDAT", compressed)
    iend = _png_chunk(b"IEND", b"")
    return header + ihdr + idat + iend


def _require_env(*names):
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        pytest.skip(f"Missing environment variables for live test: {', '.join(missing)}")
    return {name: os.environ[name] for name in names}


pytestmark = pytest.mark.live


@pytest.mark.asyncio
async def test_live_telegram_notification():
    env = _require_env("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
    notifier = TelegramNotifier(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"])

    assert await notifier.send("🧪 *Live Telegram test*\nZero-touch notifier is reachable.") is True


@pytest.mark.asyncio
async def test_live_gemini_image_ocr():
    _require_env("GEMINI_API_KEY")
    expected = "AB24"
    image_bytes = _build_test_png(expected)
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")

    result = await captcha_module._extract_gemini_text(image_b64)

    assert result.upper() == expected


@pytest.mark.asyncio
async def test_live_login_and_data_check(monkeypatch, tmp_path):
    env = _require_env(
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "SIM24_USERNAME",
        "SIM24_PASSWORD",
        "GEMINI_API_KEY",
    )
    state_path = tmp_path / "storage_state.json"
    monkeypatch.setattr(login_module, "STORAGE_STATE_PATH", state_path)

    notifier = TelegramNotifier(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"])
    login_runner = Sim24Login(env["SIM24_USERNAME"], env["SIM24_PASSWORD"], notifier)

    browser = None
    try:
        browser, page = await login_runner.login()
        assert browser is not None and page is not None

        used_kb, total_kb = await DataChecker(page).get_usage()
        assert used_kb is not None and total_kb is not None
        assert 0 <= used_kb <= total_kb
        assert state_path.exists()
    finally:
        if browser:
            await browser.close()
        pw = getattr(login_runner, "_playwright", None)
        if pw is not None:
            await pw.stop()


@pytest.mark.asyncio
async def test_live_full_workflow_safe(monkeypatch, tmp_path):
    env = _require_env(
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "SIM24_USERNAME",
        "SIM24_PASSWORD",
        "GEMINI_API_KEY",
    )
    monkeypatch.setattr(login_module, "STORAGE_STATE_PATH", tmp_path / "storage_state.json")

    class LiveConfig:
        def __init__(self):
            self.telegram_token = env["TELEGRAM_BOT_TOKEN"]
            self.telegram_chat_id = env["TELEGRAM_CHAT_ID"]
            self.sim24_username = env["SIM24_USERNAME"]
            self.sim24_password = env["SIM24_PASSWORD"]

        def record_run(self, *, success, error="", used_kb=None, total_kb=None):
            return None

        def record_usage_snapshot(self, *, used_kb, total_kb):
            return None

    sent_messages = []

    class RecordingTelegramNotifier(TelegramNotifier):
        async def send(self, text):
            sent_messages.append(text)
            return await super().send(text)

        async def send_photo(self, image_bytes, caption=""):
            sent_messages.append(caption)
            return await super().send_photo(image_bytes, caption)

    class NoBookingDecisionEngine:
        def __init__(self, threshold_gb=0.5):
            self.threshold_gb = threshold_gb

        def should_book(self, remaining_gb):
            return False

    monkeypatch.setattr(workflow_main, "ConfigManager", LiveConfig)
    monkeypatch.setattr(workflow_main, "TelegramNotifier", RecordingTelegramNotifier)
    monkeypatch.setattr(workflow_main, "DecisionEngine", NoBookingDecisionEngine)

    await workflow_main.main()

    assert any("Run complete" in message for message in sent_messages)


@pytest.mark.destructive
@pytest.mark.asyncio
async def test_live_full_workflow_destructive(monkeypatch, tmp_path):
    if os.environ.get("RUN_LIVE_BOOKING_TESTS") != "1":
        pytest.skip("Set RUN_LIVE_BOOKING_TESTS=1 to allow a real booking test.")

    force_booking = os.environ.get("SIM24_FORCE_BOOKING_TEST") == "1"

    env = _require_env(
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "SIM24_USERNAME",
        "SIM24_PASSWORD",
        "GEMINI_API_KEY",
    )
    monkeypatch.setattr(login_module, "STORAGE_STATE_PATH", tmp_path / "storage_state.json")

    class LiveConfig:
        def __init__(self):
            self.telegram_token = env["TELEGRAM_BOT_TOKEN"]
            self.telegram_chat_id = env["TELEGRAM_CHAT_ID"]
            self.sim24_username = env["SIM24_USERNAME"]
            self.sim24_password = env["SIM24_PASSWORD"]

        def record_run(self, *, success, error="", used_kb=None, total_kb=None):
            return None

        def record_usage_snapshot(self, *, used_kb, total_kb):
            return None

    sent_messages = []

    class RecordingTelegramNotifier(TelegramNotifier):
        async def send(self, text):
            sent_messages.append(text)
            return await super().send(text)

        async def send_photo(self, image_bytes, caption=""):
            sent_messages.append(caption)
            return await super().send_photo(image_bytes, caption)

    class ForceBookingDecisionEngine:
        def __init__(self, threshold_gb=0.5):
            self.threshold_gb = threshold_gb

        def should_book(self, remaining_gb):
            if force_booking:
                return True
            return remaining_gb < self.threshold_gb

    original_get_usage = DataChecker.get_usage

    async def guarded_get_usage(self):
        used_kb, total_kb = await original_get_usage(self)
        assert used_kb is not None and total_kb is not None
        remaining_gb = (total_kb - used_kb) / (1024 * 1024)
        if remaining_gb >= 0.5 and not force_booking:
            pytest.skip("Remaining data is above threshold; refusing a real booking test.")
        return used_kb, total_kb

    monkeypatch.setattr(workflow_main, "ConfigManager", LiveConfig)
    monkeypatch.setattr(workflow_main, "TelegramNotifier", RecordingTelegramNotifier)
    monkeypatch.setattr(workflow_main, "DecisionEngine", ForceBookingDecisionEngine)
    monkeypatch.setattr(workflow_main, "Sim24Login", Sim24Login)
    monkeypatch.setattr(workflow_main, "DataChecker", DataChecker)
    monkeypatch.setattr(workflow_main, "BookingModule", BookingModule)
    monkeypatch.setattr(DataChecker, "get_usage", guarded_get_usage)

    await workflow_main.main()

    assert any("2 GB packet booked successfully" in message for message in sent_messages)