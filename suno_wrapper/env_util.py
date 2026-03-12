"""Consolidated token and env file management for Suno wrapper.

Single source of truth for saving/loading tokens and JWTs across
/tmp files and ~/.env.suno. Replaces duplicated save/load logic
scattered across captcha_solver.py, solve_captcha_auto.py, etc.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# ── Standard file locations ──────────────────────────────────────────

TOKEN_FILE = Path("/tmp/suno_generate_token.txt")
CAPTCHA_FILE = Path("/tmp/suno_captcha_token.txt")
JWT_FILE = Path("/tmp/suno_jwt_fresh.txt")
ENV_FILE = Path.home() / ".env.suno"


def save_token(token: str) -> None:
    """Write captcha token to all standard locations.

    Writes to:
      - /tmp/suno_generate_token.txt
      - /tmp/suno_captcha_token.txt
      - ~/.env.suno  (SUNO_GENERATE_TOKEN key)
      - os.environ["SUNO_GENERATE_TOKEN"]
    """
    TOKEN_FILE.write_text(token)
    CAPTCHA_FILE.write_text(token)
    update_env_suno("SUNO_GENERATE_TOKEN", token)
    os.environ["SUNO_GENERATE_TOKEN"] = token


def load_token() -> str | None:
    """Load captcha token from /tmp file, validate P1_ prefix.

    Returns the token string if valid, None otherwise.
    """
    if not TOKEN_FILE.exists():
        return None
    text = TOKEN_FILE.read_text().strip()
    if text.startswith("P1_") and len(text) > 100:
        return text
    return None


def save_jwt(jwt: str) -> None:
    """Write JWT to /tmp file and ~/.env.suno.

    Writes to:
      - /tmp/suno_jwt_fresh.txt
      - ~/.env.suno  (SUNO_AUTH_TOKEN key)
      - os.environ["SUNO_AUTH_TOKEN"]
    """
    JWT_FILE.write_text(jwt)
    update_env_suno("SUNO_AUTH_TOKEN", jwt)
    os.environ["SUNO_AUTH_TOKEN"] = jwt


def load_jwt() -> str | None:
    """Load JWT from env or /tmp file.

    Checks os.environ first, then /tmp/suno_jwt_fresh.txt, then ~/.env.suno.
    Returns None if no valid JWT found.
    """
    # 1. Environment variable
    token = os.environ.get("SUNO_AUTH_TOKEN", "").strip()
    if token and token.startswith("eyJ") and len(token) > 100:
        return token

    # 2. /tmp file
    if JWT_FILE.exists():
        text = JWT_FILE.read_text().strip()
        if text.startswith("eyJ") and len(text) > 100:
            return text

    # 3. ~/.env.suno
    env = load_env_suno()
    token = env.get("SUNO_AUTH_TOKEN", "").strip()
    if token and token.startswith("eyJ") and len(token) > 100:
        return token

    return None


def load_env_suno() -> dict[str, str]:
    """Parse ~/.env.suno into a dict (key=value, ignoring comments)."""
    if not ENV_FILE.exists():
        return {}
    result: dict[str, str] = {}
    for line in ENV_FILE.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        result[key.strip()] = val.strip()
    return result


def update_env_suno(key: str, val: str) -> None:
    """Update or insert a single key in ~/.env.suno."""
    pat = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)

    if ENV_FILE.exists():
        text = ENV_FILE.read_text()
        if pat.search(text):
            text = pat.sub(f"{key}={val}", text)
        else:
            text = text.rstrip("\n") + f"\n{key}={val}\n"
        ENV_FILE.write_text(text)
    else:
        ENV_FILE.write_text(f"{key}={val}\n")
        ENV_FILE.chmod(0o600)


def reload_env_to_os() -> None:
    """Re-read key tokens from ~/.env.suno into os.environ.

    Useful at the start of each job to pick up tokens saved by other processes.
    Only updates the three critical keys used by the Suno client.

    For SUNO_AUTH_TOKEN, prefers /tmp/suno_jwt_fresh.txt (written by refresh
    daemon) over the ~/.env.suno value, since the file is always fresher.
    """
    env = load_env_suno()
    for key in ("SUNO_GENERATE_TOKEN", "SUNO_AUTH_TOKEN", "SUNO_PROJECT_ID"):
        if key in env and env[key]:
            os.environ[key] = env[key]

    # Prefer fresh JWT from file if available (written by refresh_jwt.py)
    jwt_file = Path("/tmp/suno_jwt_fresh.txt")
    if jwt_file.exists():
        fresh_jwt = jwt_file.read_text().strip()
        if fresh_jwt and len(fresh_jwt) > 100:
            os.environ["SUNO_AUTH_TOKEN"] = fresh_jwt


def env_fallback(key: str) -> str:
    """Get a value from ~/.env.suno or ~/.env, stripping quotes.

    Falls back through ~/.env.suno then ~/.env.
    """
    import shlex

    for p in (ENV_FILE, Path.home() / ".env"):
        if not p.exists():
            continue
        for line in p.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() != key:
                continue
            v = v.strip()
            try:
                return shlex.split(f"x={v}", posix=True)[0].split("=", 1)[1]
            except Exception:
                return v.strip("'\"")
    return ""
