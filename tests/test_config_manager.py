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
    monkeypatch.setenv("GIST_ID", "gist-id")


def test_load_state_from_gist(config_env, monkeypatch):
    requested = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "files": {
                    ConfigManager.GIST_FILENAME: {
                        "content": json.dumps({
                            "interval_minutes": 25,
                            "last_run_ts": 123.4,
                            "last_run_ok": False,
                            "last_run_error": "login failed",
                            "last_used_kb": 111,
                            "last_total_kb": 222,
                        })
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

    assert manager.last_run_ts == 123.4
    assert manager._state["last_run_ok"] is False
    assert manager._state["last_run_error"] == "login failed"
    assert manager._state["last_used_kb"] == 111
    assert manager._state["last_total_kb"] == 222
    assert "interval_minutes" not in manager._state
    assert requested["url"] == "https://api.github.com/gists/gist-id"
    assert requested["headers"]["Authorization"] == "Bearer gist-token"
    assert requested["timeout"] == 10


def test_load_state_falls_back_to_defaults_on_error(config_env, monkeypatch):
    def fake_get(url, headers, timeout):
        raise RuntimeError("network down")

    monkeypatch.setattr("config_manager.requests.get", fake_get)

    manager = ConfigManager()

    assert manager.last_run_ts == 0
    assert manager._state["last_run_ok"] is True
    assert manager._state["last_run_error"] == ""
    assert manager._state["last_used_kb"] is None
    assert manager._state["last_total_kb"] is None


def test_record_run_persists_status_fields_and_drops_interval_key(config_env, monkeypatch):
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
    manager.record_run(success=False, error="boom", used_kb=111, total_kb=222)

    assert len(saved) == 1
    saved_state = json.loads(saved[0]["payload"]["files"][ConfigManager.GIST_FILENAME]["content"])
    assert manager.last_run_ts == 456.7
    assert saved_state["last_run_ts"] == 456.7
    assert saved_state["last_run_ok"] is False
    assert saved_state["last_run_error"] == "boom"
    assert saved_state["last_used_kb"] == 111
    assert saved_state["last_total_kb"] == 222
    assert "interval_minutes" not in saved_state
    assert all(call["url"] == "https://api.github.com/gists/gist-id" for call in saved)


def test_record_usage_snapshot_persists_latest_known_data(config_env, monkeypatch):
    saved = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "files": {
                    ConfigManager.GIST_FILENAME: {
                        "content": json.dumps({
                            "last_run_ts": 123.4,
                            "last_used_kb": 111,
                            "last_total_kb": 222,
                        })
                    }
                }
            }

    def fake_get(url, headers, timeout):
        return FakeResponse()

    def fake_patch(url, headers, json, timeout):
        payload = json
        saved.append(__import__("json").loads(payload["files"][ConfigManager.GIST_FILENAME]["content"]))
        return FakeResponse()

    monkeypatch.setattr("config_manager.requests.get", fake_get)
    monkeypatch.setattr("config_manager.requests.patch", fake_patch)

    manager = ConfigManager()
    manager.record_usage_snapshot(used_kb=333, total_kb=444)

    assert saved == [{
        "last_run_ts": 123.4,
        "last_run_ok": True,
        "last_run_error": "",
        "last_used_kb": 333,
        "last_total_kb": 444,
        "captcha_pending": False,
        "captcha_reply": "",
        "monitoring_active": None,
    }]


def test_monitoring_active_round_trips_through_state(config_env, monkeypatch):
    saved = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "files": {
                    ConfigManager.GIST_FILENAME: {
                        "content": json.dumps({"monitoring_active": None})
                    }
                }
            }

    def fake_get(url, headers, timeout):
        return FakeResponse()

    def fake_patch(url, headers, json, timeout):
        saved.append(__import__("json").loads(json["files"][ConfigManager.GIST_FILENAME]["content"]))
        return FakeResponse()

    monkeypatch.setattr("config_manager.requests.get", fake_get)
    monkeypatch.setattr("config_manager.requests.patch", fake_patch)

    manager = ConfigManager()
    assert manager._state["monitoring_active"] is None

    manager.set_monitoring_active(True)
    manager.set_monitoring_active(False)
    manager.set_monitoring_active(None)

    assert saved[-1]["monitoring_active"] is None
    assert saved[0]["monitoring_active"] is True
    assert saved[1]["monitoring_active"] is False