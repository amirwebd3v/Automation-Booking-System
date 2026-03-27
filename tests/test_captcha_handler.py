import sys
import io
import unittest.mock
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import importlib
captcha_handler = importlib.import_module("sim24_bot.captcha_handler")
from sim24_bot.captcha_handler import CaptchaHandler


class FakePage:
    pass


class FakeTelegram:
    pass


def _make_valid_image_bytes() -> bytes:
    """Create a minimal valid PNG in memory (1x1 white pixel)."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (60, 20), color=(255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def reset_trocr_cache():
    """Ensure module-level model cache is cleared between tests."""
    captcha_handler._trocr_processor = None
    captcha_handler._trocr_model = None
    yield
    captcha_handler._trocr_processor = None
    captcha_handler._trocr_model = None


# ---------------------------------------------------------------------------
# Helper: build mock processor + model that return a given text
# ---------------------------------------------------------------------------

def _build_mocks(decoded_text: str):
    mock_processor = MagicMock()
    mock_model = MagicMock()

    # processor(images=..., return_tensors="pt").pixel_values  -> a tensor-like mock
    pixel_values_mock = MagicMock()
    mock_processor.return_value.pixel_values = pixel_values_mock

    # model.generate(pixel_values) -> generated_ids mock
    generated_ids_mock = MagicMock()
    mock_model.generate.return_value = generated_ids_mock

    # processor.batch_decode(generated_ids, skip_special_tokens=True) -> list
    mock_processor.batch_decode.return_value = [decoded_text]

    return mock_processor, mock_model


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trocr_returns_text_on_success():
    """
    When TrOCR successfully decodes a valid alphanumeric string that passes
    the sanity regex, _solve_with_trocr should return that string.
    """
    mock_processor, mock_model = _build_mocks("AB12")

    with patch("captcha_handler._load_trocr_model", return_value=(mock_processor, mock_model)):
        handler = CaptchaHandler(FakePage(), FakeTelegram())
        result = await handler._solve_with_trocr(_make_valid_image_bytes())

    assert result == "AB12"


@pytest.mark.asyncio
async def test_trocr_returns_none_on_exception():
    """
    If _load_trocr_model raises (e.g. transformers not installed / network
    unavailable), _solve_with_trocr must return None without propagating.
    """
    with patch("captcha_handler._load_trocr_model", side_effect=RuntimeError("model unavailable")):
        handler = CaptchaHandler(FakePage(), FakeTelegram())
        result = await handler._solve_with_trocr(_make_valid_image_bytes())

    assert result is None


@pytest.mark.asyncio
async def test_trocr_returns_none_on_sanity_fail():
    """
    If the model returns text that does NOT match r'^[A-Za-z0-9]{3,10}$'
    (spaces, punctuation, too long, etc.), _solve_with_trocr returns None
    so the caller falls back to the Telegram manual flow.
    """
    for bad_text in ["hello world!", "??", "A" * 11, ""]:
        mock_processor, mock_model = _build_mocks(bad_text)
        with patch("captcha_handler._load_trocr_model", return_value=(mock_processor, mock_model)):
            handler = CaptchaHandler(FakePage(), FakeTelegram())
            result = await handler._solve_with_trocr(_make_valid_image_bytes())
        assert result is None, f"Expected None for bad text '{bad_text}', got '{result}'"


@pytest.mark.asyncio
async def test_trocr_model_cached_after_first_call():
    """
    _load_trocr_model should only call TrOCRProcessor.from_pretrained and
    VisionEncoderDecoderModel.from_pretrained ONCE across multiple
    _solve_with_trocr invocations (module-level cache).
    """
    mock_processor, mock_model = _build_mocks("XY99")

    with patch("captcha_handler.TrOCRProcessor" if False else "transformers.TrOCRProcessor") as _:
        # Patch _load_trocr_model itself but count real-model invocations via
        # the module globals after the first real load.
        # Strategy: let _load_trocr_model run normally but mock from_pretrained.
        from transformers import TrOCRProcessor, VisionEncoderDecoderModel

        img_bytes = _make_valid_image_bytes()

        with patch.object(
            TrOCRProcessor, "from_pretrained", return_value=mock_processor
        ) as mock_proc_load, patch.object(
            VisionEncoderDecoderModel, "from_pretrained", return_value=mock_model
        ) as mock_model_load:
            handler = CaptchaHandler(FakePage(), FakeTelegram())

            # First call — should trigger a load
            await handler._solve_with_trocr(img_bytes)
            # Second call — should reuse the cache
            await handler._solve_with_trocr(img_bytes)

        # from_pretrained must have been called exactly once each
        assert mock_proc_load.call_count == 1, "TrOCRProcessor.from_pretrained called more than once"
        assert mock_model_load.call_count == 1, "VisionEncoderDecoderModel.from_pretrained called more than once"
