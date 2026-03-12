"""Track captcha sessions — time and generation count between captcha events.

Answers the question: "How long does a session last before Suno flags us again?"

Log format (JSONL at ``auto-generated/captcha_sessions.jsonl``):
    {"ts": 1234567890, "event": "session_start", ...}
    {"ts": 1234567891, "event": "generation_ok", ...}
    {"ts": 1234567999, "event": "captcha_hit", "gens_since_solve": 12, "elapsed_min": 42.3, ...}

Usage:
    from suno_wrapper.captcha_tracker import CaptchaTracker
    tracker = CaptchaTracker()          # or CaptchaTracker(log_path=...)
    tracker.session_start()             # after captcha solve / fresh token
    tracker.generation_ok(endpoint=...) # after each successful generation
    tracker.captcha_hit(error=...)      # on "Token validation failed"
    tracker.summary()                   # -> dict with stats
"""

import json
import time
from pathlib import Path


_DEFAULT_LOG = Path(__file__).resolve().parents[2] / "auto-generated" / "captcha_sessions.jsonl"


class CaptchaTracker:
    """Lightweight, stateful tracker for captcha-to-captcha session metrics."""

    def __init__(self, log_path: Path | None = None) -> None:
        self._log_path = log_path or _DEFAULT_LOG
        self._session_start_ts: float = 0.0
        self._generation_count: int = 0
        self._total_sessions: int = 0
        self._total_generations: int = 0
        self._total_captcha_hits: int = 0
        self._last_generation_ts: float = 0.0
        self._endpoints_used: dict[str, int] = {}
        self._error_history: list[dict] = []

    # ── Events ────────────────────────────────────────────────────────

    def session_start(self, *, reason: str = "captcha_solved") -> None:
        """Mark the start of a new captcha-free session."""
        self._session_start_ts = time.time()
        self._generation_count = 0
        self._total_sessions += 1
        self._endpoints_used = {}
        self._write({
            "event": "session_start",
            "reason": reason,
            "session_number": self._total_sessions,
        })

    def generation_ok(
        self,
        *,
        endpoint: str = "",
        title: str = "",
        track: str = "",
        genre: str = "",
    ) -> None:
        """Record a successful generation."""
        self._generation_count += 1
        self._total_generations += 1
        self._last_generation_ts = time.time()
        ep = endpoint or "unknown"
        self._endpoints_used[ep] = self._endpoints_used.get(ep, 0) + 1
        elapsed = self._elapsed_min()
        self._write({
            "event": "generation_ok",
            "gen_number": self._generation_count,
            "elapsed_min": round(elapsed, 2),
            "endpoint": ep,
            "title": title or track or "",
            "genre": genre,
        })

    def generation_error(
        self,
        *,
        error: str,
        endpoint: str = "",
        title: str = "",
        is_captcha: bool = False,
    ) -> None:
        """Record a generation failure (non-captcha). Keeps error history."""
        elapsed = self._elapsed_min()
        entry = {
            "event": "generation_error",
            "error_short": _truncate(error, 120),
            "gen_number": self._generation_count,
            "elapsed_min": round(elapsed, 2),
            "endpoint": endpoint,
            "title": title,
        }
        self._error_history.append(entry)
        # Keep last 50 errors in memory
        self._error_history = self._error_history[-50:]
        self._write(entry)

    def captcha_hit(self, *, error: str = "", endpoint: str = "") -> dict:
        """Record a captcha hit. Returns session stats for alerting."""
        self._total_captcha_hits += 1
        elapsed = self._elapsed_min()
        stats = {
            "event": "captcha_hit",
            "gens_since_solve": self._generation_count,
            "elapsed_min": round(elapsed, 2),
            "endpoint": endpoint,
            "error_short": _truncate(error, 120),
            "session_number": self._total_sessions,
            "endpoints_used": dict(self._endpoints_used),
            "lifetime_generations": self._total_generations,
            "lifetime_captcha_hits": self._total_captcha_hits,
        }
        self._write(stats)
        return stats

    def captcha_solve_attempt(self, *, method: str, endpoint: str = "") -> None:
        """Record that a captcha solve strategy is being attempted."""
        self._write({
            "event": "captcha_solve_attempt",
            "method": method,
            "endpoint": endpoint,
            "elapsed_min": round(self._elapsed_min(), 2),
            "gen_number": self._generation_count,
        })

    def captcha_solve_success(
        self, *, method: str, elapsed_s: float, endpoint: str = ""
    ) -> None:
        """Record a successful captcha solve."""
        self._write({
            "event": "captcha_solve_success",
            "method": method,
            "elapsed_s": round(elapsed_s, 2),
            "endpoint": endpoint,
            "gen_number": self._generation_count,
        })

    def captcha_solve_fail(
        self, *, method: str, elapsed_s: float, error: str, endpoint: str = ""
    ) -> None:
        """Record a failed captcha solve attempt."""
        entry = {
            "event": "captcha_solve_fail",
            "method": method,
            "elapsed_s": round(elapsed_s, 2),
            "error_short": _truncate(error, 120),
            "endpoint": endpoint,
            "gen_number": self._generation_count,
        }
        self._error_history.append(entry)
        self._error_history = self._error_history[-50:]
        self._write(entry)

    def captcha_solve_chain_result(
        self,
        *,
        success: bool,
        winning_method: str = "",
        chain_tried: list[str] | None = None,
        total_elapsed_s: float = 0.0,
        token_length: int = 0,
        errors: list[str] | None = None,
    ) -> None:
        """Record the full chain result after a solve attempt completes."""
        self._write({
            "event": "captcha_solve_chain_result",
            "success": success,
            "winning_method": winning_method,
            "chain_tried": chain_tried or [],
            "total_elapsed_s": round(total_elapsed_s, 2),
            "token_length": token_length,
            "error_summary": "; ".join(errors or [])[:300],
            "gen_number": self._generation_count,
        })

    def jwt_refresh(self, *, method: str, success: bool) -> None:
        """Record a JWT refresh attempt."""
        self._write({
            "event": "jwt_refresh",
            "method": method,
            "success": success,
            "elapsed_min": round(self._elapsed_min(), 2),
            "gen_number": self._generation_count,
        })

    # ── Queries ───────────────────────────────────────────────────────

    def summary(self) -> dict:
        """Return current session + lifetime stats."""
        elapsed = self._elapsed_min()
        rate = self._generation_count / max(elapsed / 60.0, 1 / 3600) if elapsed > 0 else 0.0
        return {
            "session_number": self._total_sessions,
            "session_generations": self._generation_count,
            "session_elapsed_min": round(elapsed, 2),
            "session_rate_per_hr": round(rate, 2),
            "lifetime_generations": self._total_generations,
            "lifetime_captcha_hits": self._total_captcha_hits,
            "endpoints_used": dict(self._endpoints_used),
        }

    @property
    def generations_since_solve(self) -> int:
        return self._generation_count

    @property
    def elapsed_minutes(self) -> float:
        return self._elapsed_min()

    # ── Internals ─────────────────────────────────────────────────────

    def _elapsed_min(self) -> float:
        if self._session_start_ts <= 0:
            return 0.0
        return (time.time() - self._session_start_ts) / 60.0

    def _write(self, obj: dict) -> None:
        obj["ts"] = int(time.time())
        obj["ts_iso"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=True) + "\n")
        except OSError:
            pass


# Module-level singleton — shared across all scripts in the same process.
tracker = CaptchaTracker()


def _truncate(s: str, maxlen: int) -> str:
    s = (s or "").strip()
    return s[:maxlen] + "..." if len(s) > maxlen else s
