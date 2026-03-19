import sys
import unittest.mock
from pathlib import Path
import pytest
import aiohttp
import os

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from captcha_handler import CaptchaHandler

class FakePage:
    pass

class FakeTelegram:
    pass

@pytest.mark.asyncio
async def test_solve_with_gemini_aiohttp_patch(monkeypatch):
    """
    Test that the aiohttp.ClientConnectorDNSError bug in google-genai
    is properly patched so we don't get 'module aiohttp has no attribute...' errors.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "FAKE_KEY_FOR_TEST")
    
    # We want to simulate the scenario where google.genai tries to catch the exception. 
    # Simply calling the method should apply the patch.
    # To test it cleanly without making a real API call or requiring google-genai to be fully functional, 
    # we can just ensure that after _solve_with_gemini runs, aiohttp has the attribute.
    
    # If the attribute exists somehow before the test (due to other tests), delete it to test the patch
    if hasattr(aiohttp, "ClientConnectorDNSError"):
        del aiohttp.ClientConnectorDNSError

    handler = CaptchaHandler(FakePage(), FakeTelegram())
    
    # Mock 'google' so it fails gracefully after the patch or just mock the Client
    with unittest.mock.patch.dict(sys.modules):
        # We can just let it run. Without a valid key and with network mocked out,
        # it normally would crash. We can mock the google.genai client to raise a client error,
        # but to prove the patch runs, we just need to ensure the attribute gets set.
        
        # We mock the genai.Client so we don't do real requests
        mock_genai = unittest.mock.MagicMock()
        monkeypatch.setitem(sys.modules, "google.genai", mock_genai)
        monkeypatch.setitem(sys.modules, "google", mock_genai)
        
        await handler._solve_with_gemini(b"fake_image_bytes")

    # The patch should have applied ClientConnectorError back into ClientConnectorDNSError
    assert hasattr(aiohttp, "ClientConnectorDNSError"), "aiohttp.ClientConnectorDNSError should be patched"
    assert aiohttp.ClientConnectorDNSError is aiohttp.ClientConnectorError

@pytest.mark.asyncio
async def test_solve_with_gemini_handles_exceptions_gracefully(monkeypatch):
    """
    Test that if the Gemini API throws a random exception, it is caught
    and returns None instead of crashing the process.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "FAKE_KEY_FOR_TEST")

    handler = CaptchaHandler(FakePage(), FakeTelegram())

    class FakeClient:
        class aio:
            class models:
                @staticmethod
                async def generate_content(*args, **kwargs):
                    raise Exception("Mocked Gemini API Error")

    with unittest.mock.patch("google.genai.Client", return_value=FakeClient()):
        result = await handler._solve_with_gemini(b"fake_image_bytes")
        assert result is None  # Should gracefully catch the exception and return None
