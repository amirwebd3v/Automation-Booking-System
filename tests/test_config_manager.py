import json

import pytest

from config_manager import ConfigManager


@pytest.fixture
def config_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.setenv("SIM24_USERNAME", "user")
    monkeypatch.setenv("SIM24_PASSWORD", "pass")
    monkeypatch.setenv("GIST_TOKEN", "gist-token")
    monkeypatch.setenv("GITHUB_GIST_ID", "gist-id")


def test_load_state_from_gist(config_env, monkeypatch):
    requested = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "files": {
                    ConfigManager.GIST_FILENAME: {
                        "content": json.dumps({"interval_minutes": 25, "last_run_ts": 123.4})
                    }
                }
            }

    def fake_get(url, headers, timeout):
        requested["url"] = url
        requested["headers"] = headers
        requested["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("config_manager.requests.get", fake_get)

    manager = ConfigManager()

    assert manager.interval_minutes == 25
    assert manager.last_run_ts == 123.4
    assert requested["url"] == "https://api.github.com/gists/gist-id"
    assert requested["headers"]["Authorization"] == "Bearer gist-token"
    assert requested["timeout"] == 10


def test_load_state_falls_back_to_defaults_on_error(config_env, monkeypatch):
    def fake_get(url, headers, timeout):
        raise RuntimeError("network down")

    monkeypatch.setattr("config_manager.requests.get", fake_get)

    manager = ConfigManager()

    assert manager.interval_minutes == 10
    assert manager.last_run_ts == 0


def test_update_last_run_and_set_interval_persist_state(config_env, monkeypatch):
    saved = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "files": {
                    ConfigManager.GIST_FILENAME: {
                        "content": json.dumps({"interval_minutes": 10, "last_run_ts": 0})
                    }
                }
            }

    def fake_get(url, headers, timeout):
        return FakeResponse()

    def fake_patch(url, headers, json, timeout):
        saved.append({
            "url": url,
            "headers": headers,
            "payload": json,
            "timeout": timeout,
        })
        return FakeResponse()

    monkeypatch.setattr("config_manager.requests.get", fake_get)
    monkeypatch.setattr("config_manager.requests.patch", fake_patch)
    monkeypatch.setattr("config_manager.time.time", lambda: 456.7)

    manager = ConfigManager()
    manager.update_last_run()
    manager.set_interval(2)

    assert len(saved) == 2
    assert saved[0]["payload"]["files"][ConfigManager.GIST_FILENAME]["content"]
    assert manager.last_run_ts == 456.7
    assert manager.interval_minutes == 5
    assert all(call["url"] == "https://api.github.com/gists/gist-id" for call in saved)