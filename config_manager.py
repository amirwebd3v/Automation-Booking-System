"""
Config Manager
─────────────
Reads credentials from environment variables (GitHub Secrets / .env).
Stores and retrieves dynamic state via a GitHub Gist so values persist
across GitHub Actions runs.

Gist state keys:
  interval_minutes  — minimum minutes between booking pipeline runs
  last_run_ts       — Unix timestamp of the last completed run
  captcha_pending   — True while waiting for a human captcha reply
  captcha_reply     — text entered by the human; consumed by captcha_handler
"""

import os
import json
import time
import requests
from datetime import datetime, timezone


class ConfigManager:
    # ── Gist file name used as our tiny key-value store ──────────────────────
    GIST_FILENAME = "sim24_bot_config.json"

    def __init__(self):
        # Required secrets (set in GitHub Actions → Secrets)
        self.telegram_token   = os.environ["TELEGRAM_BOT_TOKEN"]
        self.telegram_chat_id = os.environ["TELEGRAM_CHAT_ID"]
        self.sim24_username   = os.environ["SIM24_USERNAME"]
        self.sim24_password   = os.environ["SIM24_PASSWORD"]
        self.github_token     = os.environ["GIST_TOKEN"]
        self.gist_id          = os.environ["GIST_ID"]

        # Load dynamic config from Gist
        self._state = self._load_state()

    # ── Public properties ─────────────────────────────────────────────────────

    @property
    def interval_minutes(self) -> int:
        return int(self._state.get("interval_minutes", 10))

    @property
    def last_run_ts(self) -> float:
        return float(self._state.get("last_run_ts", 0))

    # ── Timing logic ──────────────────────────────────────────────────────────

    def is_time_to_run(self) -> bool:
        """Returns True if enough time has elapsed since last successful run."""
        now = time.time()
        elapsed_seconds = now - self.last_run_ts
        required_seconds = self.interval_minutes * 60
        return elapsed_seconds >= required_seconds

    def update_last_run(self):
        """Saves current timestamp as last_run to Gist."""
        self._state["last_run_ts"] = time.time()
        try:
            self._save_state()
        except Exception:
            pass  # Non-critical; a missed timestamp is acceptable

    def set_interval(self, minutes: int):
        """Called by scheduler bot to change check interval."""
        self._state["interval_minutes"] = max(5, minutes)  # Minimum 5 min (default 10)
        try:
            self._save_state()
        except Exception:
            pass  # Caller (scheduler bot) uses save_gist directly and handles errors itself

    def set_captcha_pending(self, pending: bool) -> None:
        """Signal the scheduler bot that a CAPTCHA is waiting for manual input."""
        self._state["captcha_pending"] = pending
        if not pending:
            self._state.pop("captcha_reply", None)
        self._save_state()

    def get_captcha_reply(self) -> "str | None":
        """Reload Gist and return the captcha reply written by the scheduler bot."""
        self._state = self._load_state()
        return self._state.get("captcha_reply")

    def clear_captcha_state(self) -> None:
        """Remove captcha_pending and captcha_reply from Gist."""
        self._state.pop("captcha_pending", None)
        self._state.pop("captcha_reply", None)
        self._save_state()

    # ── Gist persistence ──────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        """Fetch current config state from GitHub Gist."""
        try:
            headers = {
                "Authorization": f"Bearer {self.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            resp = requests.get(
                f"https://api.github.com/gists/{self.gist_id}",
                headers=headers,
                timeout=10
            )
            resp.raise_for_status()
            content = resp.json()["files"][self.GIST_FILENAME]["content"]
            return json.loads(content)
        except Exception as e:
            print(f"[CONFIG] Could not load Gist state: {e}. Using defaults.")
            return {
                "interval_minutes": 10,
                "last_run_ts": 0
            }

    def _save_state(self):
        """Persist current state back to GitHub Gist."""
        try:
            headers = {
                "Authorization": f"Bearer {self.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            payload = {
                "files": {
                    self.GIST_FILENAME: {
                        "content": json.dumps(self._state, indent=2)
                    }
                }
            }
            resp = requests.patch(
                f"https://api.github.com/gists/{self.gist_id}",
                headers=headers,
                json=payload,
                timeout=10
            )
            resp.raise_for_status()
            print(f"[CONFIG] State saved to Gist: {self._state}")
        except Exception as e:
            status = getattr(getattr(e, 'response', None), 'status_code', None)
            if status == 403:
                msg = ("[CONFIG] ⚠️ Gist save failed (403 Forbidden). "
                       "Ensure GIST_TOKEN is a classic PAT with the 'gist' scope — "
                       "fine-grained PATs do not support the Gist API.")
            elif status == 404:
                msg = f"[CONFIG] ⚠️ Gist save failed (404 Not Found). Check GIST_ID is correct."
            else:
                msg = f"[CONFIG] Could not save Gist state: {e}"
            print(msg)
            raise RuntimeError(msg) from e
