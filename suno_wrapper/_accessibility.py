"""Accessibility cookie manager for hCaptcha bypass.

When an accessibility cookie is active, hCaptcha auto-solves without visual
challenges. The cookie is obtained once and persisted; it lasts ~23 hours.

This module injects the cookie into the BrowserOS session before
captcha solve attempts.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


DEFAULT_COOKIE_FILE = Path.home() / ".suno_hcaptcha_accessibility_cookie"
COOKIE_MAX_AGE_HOURS = 23


class AccessibilityCookieManager:
    """Manage the hCaptcha accessibility cookie for BrowserOS sessions."""

    def __init__(
        self,
        *,
        browseros_port: int = 9200,
        cookie_file: Path | None = None,
        verbose: bool = True,
    ) -> None:
        self._browseros_port = int(
            os.environ.get("BROWSEROS_MCP_PORT", str(browseros_port))
        )
        self._cookie_file = cookie_file or DEFAULT_COOKIE_FILE
        self._verbose = verbose

    async def ensure_cookie(self) -> bool:
        """Check if a valid accessibility cookie exists."""
        return self.is_cookie_valid()

    async def inject_cookie(self) -> bool:
        """Inject the persisted accessibility cookie into the BrowserOS session.

        Returns True if the cookie was successfully injected.
        """
        if not self.is_cookie_valid():
            if self._verbose:
                print("  [accessibility] no valid cookie to inject")
            return False

        cookie_data = self._load_cookie()
        if not cookie_data:
            return False

        cookie_value = cookie_data.get("value", "")
        if not cookie_value:
            return False

        try:
            import httpx

            # Set cookie via BrowserOS MCP
            js_code = (
                f"document.cookie = 'hc_accessibility={cookie_value}; "
                f"domain=.hcaptcha.com; path=/; max-age={COOKIE_MAX_AGE_HOURS * 3600}; "
                f"secure; SameSite=None';"
            )
            resp = httpx.post(
                f"http://127.0.0.1:{self._browseros_port}/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "browser_execute_javascript",
                        "arguments": {
                            "tabId": 0,  # Any tab — cookie is domain-wide
                            "code": js_code,
                        },
                    },
                },
                headers={"Content-Type": "application/json"},
                timeout=10.0,
            )
            if self._verbose:
                print(f"  [accessibility] cookie injected (status={resp.status_code})")
            return resp.status_code == 200

        except Exception as exc:
            if self._verbose:
                print(f"  [accessibility] inject error: {exc}")
            return False

    def is_cookie_valid(self) -> bool:
        """Check if the persisted cookie exists and is less than 23 hours old."""
        cookie_data = self._load_cookie()
        if not cookie_data:
            return False

        created_at = cookie_data.get("created_at", 0)
        if not created_at:
            return False

        age_hours = (time.time() - created_at) / 3600
        return age_hours < COOKIE_MAX_AGE_HOURS

    def save_cookie(self, value: str) -> None:
        """Persist a new accessibility cookie."""
        data = {
            "value": value,
            "created_at": time.time(),
            "created_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        try:
            self._cookie_file.parent.mkdir(parents=True, exist_ok=True)
            self._cookie_file.write_text(
                json.dumps(data, indent=2, ensure_ascii=False)
            )
            self._cookie_file.chmod(0o600)
        except OSError as exc:
            if self._verbose:
                print(f"  [accessibility] save error: {exc}")

    def _load_cookie(self) -> dict | None:
        """Load cookie data from file."""
        if not self._cookie_file.exists():
            return None
        try:
            return json.loads(self._cookie_file.read_text())
        except (json.JSONDecodeError, OSError):
            return None
