"""
Config Manager
─────────────
Reads credentials from environment variables (GitHub Secrets / .env).
Stores and retrieves dynamic state via a GitHub Gist so values persist
across GitHub Actions runs.

Gist state keys:
    last_run_ts       — Unix timestamp of the last completed run
    last_run_ok       — whether the last run completed successfully
    last_run_error    — human-readable error text for the last failed run
    last_used_kb      — last known used data volume in KB
    last_total_kb     — last known total data volume in KB
    captcha_pending   — True while waiting for a human captcha reply
    captcha_reply     — text entered by the human; consumed by captcha_handler
"""

import os
import json
import time
import requests


class ConfigManager:
    # ── Gist file name used as our tiny key-value store ──────────────────────
    GIST_FILENAME = "sim24_bot_config.json"
    DEFAULT_STATE = {
        "last_run_ts": 0,
        "last_run_ok": True,
        "last_run_error": "",
        "last_used_kb": None,
        "last_total_kb": None,
        "captcha_pending": False,
        "captcha_reply": "",
        "monitoring_active": None,  # None=auto, True=forced on, False=forced off
    }

    def __init__(self):
        # Required secrets (set in GitHub Actions → Secrets)
        self.telegram_token   = os.environ["TELEGRAM_BOT_TOKEN"]
        self.telegram_chat_id = os.environ["TELEGRAM_CHAT_ID"]
        self.sim24_username   = os.environ["SIM24_USERNAME"]
        self.sim24_password   = os.environ["SIM24_PASSWORD"]
        self.github_token     = os.environ["GIST_TOKEN"]
        self.gist_id          = os.environ["GIST_ID"]

        # Load dynamic config from Gist
        self._state = self._normalize_state(self._load_state())

    # ── Public properties ─────────────────────────────────────────────────────

    @property
    def last_run_ts(self) -> float:
        return float(self._state.get("last_run_ts", 0))

    def update_last_run(self):
        """Backwards-compatible helper that marks a run as completed successfully."""
        self.record_run(success=True)

    def record_run(
        self,
        *,
        success: bool,
        error: str = "",
        used_kb: int | None = None,
        total_kb: int | None = None,
    ) -> None:
        """Persist the latest workflow result for status reporting."""
        self._state["last_run_ts"] = time.time()
        self._state["last_run_ok"] = bool(success)
        self._state["last_run_error"] = error or ""
        if used_kb is not None:
            self._state["last_used_kb"] = int(used_kb)
        if total_kb is not None:
            self._state["last_total_kb"] = int(total_kb)
        self._state.pop("interval_minutes", None)
        try:
            self._save_state()
        except Exception:
            pass  # Non-critical; the workflow should not fail because status could not persist

    def record_usage_snapshot(self, *, used_kb: int, total_kb: int) -> None:
        """Persist the latest successfully read usage values for status reporting."""
        self._state["last_used_kb"] = int(used_kb)
        self._state["last_total_kb"] = int(total_kb)
        self._state.pop("interval_minutes", None)
        try:
            self._save_state()
        except Exception:
            pass  # Best effort only; a later record_run call may still persist the snapshot

    def set_captcha_pending(self, pending: bool) -> None:
        """Signal the scheduler bot that a CAPTCHA is waiting for manual input."""
        self._state["captcha_pending"] = pending
        if not pending:
            self._state.pop("captcha_reply", None)
        self._save_state()

    def set_monitoring_active(self, active: "bool | None") -> None:
        """Set monitoring mode: True=forced on, False=forced off, None=auto (threshold-based)."""
        self._state["monitoring_active"] = active
        self._save_state()

    def get_captcha_reply(self) -> "str | None":
        """Reload Gist and return the captcha reply written by the scheduler bot."""
        self._state = self._normalize_state(self._load_state())
        return self._state.get("captcha_reply")

    def clear_captcha_state(self) -> None:
        """Remove captcha_pending and captcha_reply from Gist."""
        self._state.pop("captcha_pending", None)
        self._state.pop("captcha_reply", None)
        self._save_state()

    # ── Gist persistence ──────────────────────────────────────────────────────

    @classmethod
    def _default_state(cls) -> dict:
        return dict(cls.DEFAULT_STATE)

    @classmethod
    def _normalize_state(cls, state: dict | None) -> dict:
        normalized = cls._default_state()
        if state:
            normalized.update(state)

        normalized.pop("interval_minutes", None)
        normalized["last_run_ts"] = float(normalized.get("last_run_ts", 0) or 0)
        normalized["last_run_ok"] = bool(normalized.get("last_run_ok", True))
        normalized["last_run_error"] = str(normalized.get("last_run_error", "") or "")

        for key in ("last_used_kb", "last_total_kb"):
            value = normalized.get(key)
            if value is None:
                continue
            try:
                normalized[key] = int(float(value))
            except (TypeError, ValueError):
                normalized[key] = None

        normalized["captcha_pending"] = bool(normalized.get("captcha_pending", False))
        normalized["captcha_reply"] = str(normalized.get("captcha_reply", "") or "")
        ma = normalized.get("monitoring_active")
        normalized["monitoring_active"] = None if ma is None else bool(ma)
        return normalized

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
            return self._default_state()

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
