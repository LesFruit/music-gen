"""Pre-batch validation checks for Suno wrapper.

Run before any batch to catch issues early — BrowserOS down, JWT expired,
missing env vars, etc.

Usage:
    from suno_wrapper.preflight import run_preflight

    results = await run_preflight()
    for r in results:
        print(f"{'OK' if r.ok else 'FAIL'} {r.check}: {r.message}")
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .env_util import ENV_FILE, JWT_FILE, TOKEN_FILE, load_env_suno, load_jwt


@dataclass
class PreflightResult:
    """Result of a single preflight check."""

    check: str
    ok: bool
    message: str


ALL_CHECKS = [
    "browseros",
    "jwt",
    "env_vars",
    "captcha_token",
    "suno_api",
    "disk_space",
]


async def run_preflight(
    checks: list[str] | None = None,
    output_dir: Path | None = None,
) -> list[PreflightResult]:
    """Run preflight checks and return results.

    Args:
        checks: List of check names to run. None = all checks.
        output_dir: Directory to check for disk space. Defaults to auto-generated/.
    """
    if checks is None:
        checks = ALL_CHECKS

    results: list[PreflightResult] = []
    for name in checks:
        fn = _CHECK_MAP.get(name)
        if fn is None:
            results.append(PreflightResult(check=name, ok=False, message="Unknown check"))
            continue
        try:
            if name == "disk_space":
                results.append(fn(output_dir or Path("auto-generated")))
            else:
                results.append(fn())
        except Exception as e:
            results.append(PreflightResult(check=name, ok=False, message=f"Exception: {e}"))
    return results


def check_browseros() -> PreflightResult:
    """Check if BrowserOS MCP server is reachable."""
    port = int(os.environ.get("BROWSEROS_MCP_PORT", "9200"))
    try:
        import httpx

        r = httpx.get(f"http://127.0.0.1:{port}/mcp", timeout=5.0)
        if r.status_code < 500:
            return PreflightResult(check="browseros", ok=True, message=f"BrowserOS up on :{port}")
    except ImportError:
        # httpx not available — try urllib
        import urllib.request

        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp")
            urllib.request.urlopen(req, timeout=5)
            return PreflightResult(check="browseros", ok=True, message=f"BrowserOS up on :{port}")
        except Exception as e:
            return PreflightResult(check="browseros", ok=False, message=f"BrowserOS unreachable on :{port}: {e}")
    except Exception as e:
        return PreflightResult(check="browseros", ok=False, message=f"BrowserOS unreachable on :{port}: {e}")
    return PreflightResult(check="browseros", ok=False, message=f"BrowserOS returned error on :{port}")


def check_jwt() -> PreflightResult:
    """Check JWT exists and isn't about to expire (<10min remaining)."""
    jwt = load_jwt()
    if not jwt:
        return PreflightResult(check="jwt", ok=False, message="No valid JWT found in env or /tmp")

    try:
        # Decode JWT payload (no verification)
        payload_b64 = jwt.split(".")[1] + "=="
        data = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = data.get("exp", 0)
        remaining = exp - int(time.time())
        if remaining < 600:
            return PreflightResult(
                check="jwt", ok=False,
                message=f"JWT expires in {remaining}s (< 10min). Refresh recommended.",
            )
        return PreflightResult(
            check="jwt", ok=True,
            message=f"JWT valid, {remaining // 60}min remaining",
        )
    except Exception:
        # Can't decode but token exists
        return PreflightResult(check="jwt", ok=True, message="JWT present (could not decode expiry)")


def check_env_vars() -> PreflightResult:
    """Check required environment variables are set."""
    env = load_env_suno()
    missing = []

    for key in ("SUNO_AUTH_TOKEN", "SUNO_DEVICE_ID"):
        val = os.environ.get(key, "").strip() or env.get(key, "").strip()
        if not val:
            missing.append(key)

    if missing:
        return PreflightResult(
            check="env_vars", ok=False,
            message=f"Missing: {', '.join(missing)}",
        )
    return PreflightResult(check="env_vars", ok=True, message="All required env vars present")


def check_captcha_token() -> PreflightResult:
    """Check if a valid P1_ captcha token file exists."""
    if not TOKEN_FILE.exists():
        return PreflightResult(
            check="captcha_token", ok=False,
            message=f"{TOKEN_FILE} not found (will need solve on first captcha gate)",
        )
    text = TOKEN_FILE.read_text().strip()
    if text.startswith("P1_") and len(text) > 100:
        return PreflightResult(
            check="captcha_token", ok=True,
            message=f"Valid P1_ token ({len(text)} chars)",
        )
    return PreflightResult(
        check="captcha_token", ok=False,
        message=f"Token file exists but invalid (len={len(text)}, prefix={text[:4]!r})",
    )


def check_suno_api() -> PreflightResult:
    """Quick health check against Suno API (/api/session)."""
    jwt = load_jwt()
    if not jwt:
        return PreflightResult(check="suno_api", ok=False, message="No JWT — cannot check API")

    try:
        import httpx

        r = httpx.get(
            "https://studio-api.prod.suno.com/api/session",
            headers={"Authorization": f"Bearer {jwt}"},
            timeout=10.0,
        )
        if r.status_code == 200:
            return PreflightResult(check="suno_api", ok=True, message="Suno API responded 200")
        return PreflightResult(
            check="suno_api", ok=False,
            message=f"Suno API returned {r.status_code}",
        )
    except ImportError:
        return PreflightResult(check="suno_api", ok=False, message="httpx not available")
    except Exception as e:
        return PreflightResult(check="suno_api", ok=False, message=f"Suno API error: {e}")


def check_disk_space(output_dir: Path = Path("auto-generated")) -> PreflightResult:
    """Check at least 1GB free on the output directory's filesystem."""
    check_path = output_dir if output_dir.exists() else Path.home()
    usage = shutil.disk_usage(str(check_path))
    free_gb = usage.free / (1024 ** 3)
    if free_gb < 1.0:
        return PreflightResult(
            check="disk_space", ok=False,
            message=f"Only {free_gb:.1f}GB free (need >1GB)",
        )
    return PreflightResult(check="disk_space", ok=True, message=f"{free_gb:.1f}GB free")


def format_results(results: list[PreflightResult]) -> str:
    """Format preflight results as a human-readable string."""
    lines = []
    all_ok = all(r.ok for r in results)
    for r in results:
        icon = "OK" if r.ok else "FAIL"
        lines.append(f"  [{icon}] {r.check}: {r.message}")
    header = "Preflight: ALL PASSED" if all_ok else "Preflight: SOME CHECKS FAILED"
    return f"  {header}\n" + "\n".join(lines)


# ── Check registry ──────────────────────────────────────────────────

_CHECK_MAP = {
    "browseros": check_browseros,
    "jwt": check_jwt,
    "env_vars": check_env_vars,
    "captcha_token": check_captcha_token,
    "suno_api": check_suno_api,
    "disk_space": check_disk_space,
}
