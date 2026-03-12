"""Single source of truth for browser identity and anti-detection helpers.

Every HTTP request to Suno (client.py, suno_auth.py, etc.) should use these
constants and helpers so the fingerprint is consistent across the session.
"""

import asyncio
import os
import random
import uuid
from pathlib import Path

# ── Browser identity constants ──────────────────────────────────────────
# Matches a real Chrome 133 on macOS — keep UA, Sec-CH-UA, and platform
# in sync.  Update all three together when bumping the version.

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36"
)

SEC_CH_UA = '"Chromium";v="133", "Google Chrome";v="133", "Not(A:Brand";v="24"'
SEC_CH_UA_PLATFORM = '"macOS"'
SEC_CH_UA_MOBILE = "?0"

DEFAULT_DEVICE_ID_FILE = Path.home() / ".suno_device_id"


def get_browser_headers(device_id: str | None = None) -> dict[str, str]:
    """Return a consistent set of browser-like HTTP headers.

    When *device_id* is provided it is included as both ``device-id`` and
    ``Device-Id`` (Suno uses both spellings).  Omit it for requests that
    don't need a device identifier (e.g. Clerk token refresh).
    """
    headers: dict[str, str] = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Origin": "https://suno.com",
        "Referer": "https://suno.com/",
        "Sec-CH-UA": SEC_CH_UA,
        "Sec-CH-UA-Platform": SEC_CH_UA_PLATFORM,
        "Sec-CH-UA-Mobile": SEC_CH_UA_MOBILE,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "Sec-GPC": "1",
    }
    if device_id:
        headers["device-id"] = device_id
        headers["Device-Id"] = device_id
    return headers


def get_device_id(
    explicit: str | None = None,
    *,
    persist_path: Path | None = None,
) -> str:
    """Return a stable device ID, persisted across sessions.

    Resolution order:
    1. *explicit* parameter (passed directly by caller)
    2. ``SUNO_DEVICE_ID`` environment variable
    3. Value read from *persist_path* (default ``~/.suno_device_id``)
    4. Generate a new UUID4, write it to *persist_path*, return it
    """
    if explicit and explicit.strip():
        return explicit.strip()

    env_val = os.environ.get("SUNO_DEVICE_ID", "").strip()
    if env_val:
        return env_val

    path = persist_path or DEFAULT_DEVICE_ID_FILE
    if path.exists():
        stored = path.read_text().strip()
        if stored:
            return stored

    new_id = str(uuid.uuid4())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_id)
    except OSError:
        pass  # best-effort persistence
    return new_id


def jitter(base_seconds: float, spread: float = 0.3) -> float:
    """Return *base_seconds* ± *spread* fraction of randomness.

    ``jitter(10, 0.3)`` → uniform random in ``[7.0, 13.0]``.
    A non-positive *base_seconds* returns ``0.0``.
    """
    if base_seconds <= 0:
        return 0.0
    low = base_seconds * (1 - spread)
    high = base_seconds * (1 + spread)
    return random.uniform(low, high)


# Realistic delay ranges by action type (seconds).
_HUMAN_DELAY_RANGES: dict[str, tuple[float, float]] = {
    "click": (0.08, 0.25),
    "type": (0.04, 0.12),
    "page_load": (1.5, 4.0),
    "captcha_interact": (0.5, 1.5),
    "scroll": (0.3, 0.8),
    "default": (0.2, 0.6),
}


async def human_delay(action: str = "default", spread: float = 0.3) -> float:
    """Sleep for a realistic human-like duration based on *action* type.

    Returns the actual delay applied (seconds).
    """
    lo, hi = _HUMAN_DELAY_RANGES.get(action, _HUMAN_DELAY_RANGES["default"])
    # Apply spread to widen/narrow the range
    mid = (lo + hi) / 2
    lo = max(0.01, mid * (1 - spread))
    hi = mid * (1 + spread)
    delay = random.uniform(lo, hi)
    await asyncio.sleep(delay)
    return delay
