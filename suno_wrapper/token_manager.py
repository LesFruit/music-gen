"""Token lifecycle manager with adaptive reload cadence.

Tracks usage of captcha (P1_) tokens and adapts the max_uses limit
based on observed success/failure patterns. Replaces hardcoded
GENS_BEFORE_RELOAD constants in batch scripts.

Grace period extension strategy:
  After page reload, Suno allows ~7-12 API calls with token=null.
  This manager tracks how many succeed and adapts max_uses accordingly.
  When approaching the limit, it can signal the caller to reload early
  (before hitting "Token validation failed") to maintain a smooth flow.

Usage:
    from suno_wrapper.token_manager import TokenManager
    mgr = TokenManager()

    for generation in jobs:
        if mgr.should_reload():
            reload_page()
            mgr.reset()
        token = mgr.use()
        try:
            result = generate(token=token)
            mgr.record_success()
        except TokenValidationError:
            mgr.record_failure()
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .env_util import load_token, save_token

CONFIG_FILE = Path.home() / ".suno_token_config.json"

DEFAULT_MAX_USES = 7
MIN_MAX_USES = 4
MAX_MAX_USES = 15
# Reload 1 call early to avoid hitting "Token validation failed"
RELOAD_BUFFER = 1


@dataclass
class TokenManager:
    """Manages captcha token lifecycle with adaptive usage limits."""

    token: str | None = None
    uses: int = 0
    max_uses: int = DEFAULT_MAX_USES
    acquired_at: float = 0.0
    source: str = "unknown"  # "captcha_solve" | "file" | "grace_period"
    _successes_this_session: int = field(default=0, repr=False)
    _failures_this_session: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        self._load_config()

    def use(self) -> str | None:
        """Increment counter and return token (or None if exhausted/empty).

        In grace period mode (source="grace_period"), token is None but
        uses still counts toward the reload cadence.
        """
        if self.is_exhausted():
            return None
        self.uses += 1
        return self.token

    def is_exhausted(self) -> bool:
        """True if we've used up all allowed calls for this token/session."""
        return self.uses >= self.max_uses

    def should_reload(self) -> bool:
        """True if we should proactively reload BEFORE hitting the wall.

        Reloads 1 call early to avoid the "Token validation failed" error
        and the cost of a captcha solve. This is the recommended check
        instead of is_exhausted() for batch scripts.
        """
        return self.uses >= max(1, self.max_uses - RELOAD_BUFFER)

    def remaining(self) -> int:
        """Number of calls left before exhaustion."""
        return max(0, self.max_uses - self.uses)

    def record_success(self) -> None:
        """Record that the last API call succeeded with this token."""
        self._successes_this_session += 1

    def record_failure(self) -> None:
        """Record a 'Token validation failed' error.

        Immediately marks token as exhausted and shrinks max_uses.
        """
        self._failures_this_session += 1
        self.uses = self.max_uses  # force exhaustion

    def reset(self, token: str | None = None, source: str = "grace_period") -> None:
        """Reset counter for a new page reload or new token.

        Args:
            token: New captcha token (or None for grace period).
            source: Where the token came from.
        """
        self.token = token
        self.uses = 0
        self.source = source
        self.acquired_at = time.time()
        self._successes_this_session = 0
        self._failures_this_session = 0

    def set_token(self, token: str, source: str = "captcha_solve") -> None:
        """Set a fresh captcha token and reset counters."""
        self.reset(token=token, source=source)
        save_token(token)

    def load_from_file(self) -> str | None:
        """Try to load a valid token from disk."""
        token = load_token()
        if token:
            self.reset(token=token, source="file")
        return token

    def adapt(self) -> None:
        """Adjust max_uses based on this session's results and persist.

        Call this when a session ends (before page reload or new solve).
        - All calls succeeded → bump max_uses by 1
        - Had failures → shrink max_uses by 1
        """
        if self._failures_this_session > 0:
            self.max_uses = max(MIN_MAX_USES, self.max_uses - 1)
        elif self._successes_this_session >= self.max_uses:
            self.max_uses = min(MAX_MAX_USES, self.max_uses + 1)
        self._save_config()

    def status(self) -> dict:
        """Return a snapshot dict suitable for logging."""
        return {
            "token_uses": self.uses,
            "max_uses": self.max_uses,
            "remaining": self.remaining(),
            "source": self.source,
            "exhausted": self.is_exhausted(),
        }

    # ── Persistence ──────────────────────────────────────────────────

    def _load_config(self) -> None:
        """Load adaptive max_uses from ~/.suno_token_config.json."""
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text())
                stored = data.get("max_uses", DEFAULT_MAX_USES)
                if MIN_MAX_USES <= stored <= MAX_MAX_USES:
                    self.max_uses = stored
            except (json.JSONDecodeError, TypeError, KeyError):
                pass

    def _save_config(self) -> None:
        """Persist adaptive max_uses and session history to ~/.suno_token_config.json."""
        # Load existing history
        history: list[dict] = []
        if CONFIG_FILE.exists():
            try:
                old = json.loads(CONFIG_FILE.read_text())
                history = old.get("history", [])
            except Exception:
                pass

        # Append this session's result (keep last 20)
        if self._successes_this_session > 0 or self._failures_this_session > 0:
            history.append({
                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "ok": self._successes_this_session,
                "fail": self._failures_this_session,
                "max_uses": self.max_uses,
                "source": self.source,
            })
            history = history[-20:]

        data = {
            "max_uses": self.max_uses,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "history": history,
        }
        try:
            CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n")
        except OSError:
            pass
