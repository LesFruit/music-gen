"""Centralized multi-strategy hCaptcha solver for Suno.

Chain order: file_token -> accessibility -> browseros_auto -> browseros_vision -> capsolver -> cdp_visual -> vnc_manual

Each strategy is tried in sequence until one succeeds or all fail.

Usage:
    from suno_wrapper.captcha_solver import CaptchaSolver, SolveMethod, SolveResult

    solver = CaptchaSolver()
    result = await solver.solve()
    if result.success:
        print(f"Got token via {result.method}: {result.token[:40]}...")
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .captcha_tracker import CaptchaTracker

# ── Constants ────────────────────────────────────────────────────────────

HCAPTCHA_SITEKEY = "d65453de-3f1a-4aac-9366-a0f06e52b2ce"
HCAPTCHA_PAGE_URL = "https://suno.com/create"

# Standard token file locations
TOKEN_FIFO_FILE = Path("/tmp/suno_captcha_tokens.txt")
TOKEN_SINGLE_FILE = Path("/tmp/suno_captcha_token.txt")
GENERATE_TOKEN_FILE = Path("/tmp/suno_generate_token.txt")
ENV_SUNO_FILE = Path.home() / ".env.suno"


class SolveMethod(str, Enum):
    """Available captcha solving strategies."""
    FILE_TOKEN = "file_token"
    ACCESSIBILITY = "accessibility"
    BROWSEROS_AUTO = "browseros_auto"
    BROWSEROS_VISION = "browseros_vision"
    YOLO_LOCAL = "yolo_local"
    CAPSOLVER = "capsolver"
    CDP_VISUAL = "cdp_visual"
    VNC_MANUAL = "vnc_manual"


DEFAULT_CHAIN = [
    SolveMethod.FILE_TOKEN,
    SolveMethod.ACCESSIBILITY,
    SolveMethod.BROWSEROS_AUTO,
    SolveMethod.BROWSEROS_VISION,
    SolveMethod.CAPSOLVER,
    SolveMethod.CDP_VISUAL,
    SolveMethod.VNC_MANUAL,
]


@dataclass
class SolveResult:
    """Result of a captcha solve attempt."""
    token: str = ""
    method: SolveMethod | None = None
    elapsed_seconds: float = 0.0
    success: bool = False
    error: str = ""


def _env_fallback(key: str) -> str:
    """Read a key from ~/.env.suno or ~/.env (standalone, no external deps)."""
    for p in (Path.home() / ".env.suno", Path.home() / ".env"):
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


class CaptchaSolver:
    """Multi-strategy hCaptcha solver with configurable chain order."""

    def __init__(
        self,
        *,
        capsolver_api_key: str = "",
        browseros_port: int = 9200,
        cdp_port: int = 0,
        token_file: Path | None = None,
        chain: list[SolveMethod] | None = None,
        tracker: CaptchaTracker | None = None,
        verbose: bool = True,
    ) -> None:
        self._capsolver_api_key = (
            capsolver_api_key
            or os.environ.get("CAPSOLVER_API_KEY", "").strip()
            or _env_fallback("CAPSOLVER_API_KEY")
        )
        self._browseros_port = int(
            os.environ.get("BROWSEROS_MCP_PORT", str(browseros_port))
        )
        self._cdp_port = cdp_port
        self._token_file = token_file or TOKEN_FIFO_FILE
        self._chain = chain if chain is not None else list(DEFAULT_CHAIN)
        self._tracker = tracker
        self._verbose = verbose

    # ── Public API ───────────────────────────────────────────────────

    async def solve(
        self,
        *,
        methods: list[SolveMethod] | None = None,
        timeout: float = 180.0,
    ) -> SolveResult:
        """Try each method in the chain until one succeeds or all fail."""
        chain = methods or self._chain
        deadline = time.monotonic() + timeout
        last_error = ""
        chain_t0 = time.monotonic()
        methods_tried: list[str] = []
        method_errors: list[str] = []

        if self._verbose:
            self._log(f"solve chain: {[m.value for m in chain]}")

        for method in chain:
            if time.monotonic() >= deadline:
                break

            remaining = deadline - time.monotonic()
            methods_tried.append(method.value)
            if self._tracker:
                self._tracker.captcha_solve_attempt(
                    method=method.value, endpoint="captcha_solver"
                )

            t0 = time.monotonic()
            try:
                result = await self._dispatch(method, remaining)
            except Exception as exc:
                elapsed = time.monotonic() - t0
                last_error = f"{method.value}: {exc}"
                method_errors.append(f"{method.value}: {exc}")
                if self._verbose:
                    self._log(f"[{method.value}] error: {exc}")
                if self._tracker:
                    self._tracker.captcha_solve_fail(
                        method=method.value,
                        elapsed_s=round(elapsed, 2),
                        error=str(exc),
                        endpoint="captcha_solver",
                    )
                continue

            if result.success:
                if self._tracker:
                    self._tracker.captcha_solve_success(
                        method=method.value,
                        elapsed_s=round(result.elapsed_seconds, 2),
                        endpoint="captcha_solver",
                    )
                    self._tracker.captcha_solve_chain_result(
                        success=True,
                        winning_method=method.value,
                        chain_tried=methods_tried,
                        total_elapsed_s=time.monotonic() - chain_t0,
                        token_length=len(result.token),
                        errors=method_errors,
                    )
                return result

            last_error = result.error or f"{method.value}: no token"
            method_errors.append(last_error)
            if self._tracker:
                self._tracker.captcha_solve_fail(
                    method=method.value,
                    elapsed_s=round(result.elapsed_seconds, 2),
                    error=last_error,
                    endpoint="captcha_solver",
                )

        # Log full chain failure
        if self._tracker:
            self._tracker.captcha_solve_chain_result(
                success=False,
                chain_tried=methods_tried,
                total_elapsed_s=time.monotonic() - chain_t0,
                errors=method_errors,
            )

        return SolveResult(
            success=False,
            error=last_error or "all methods exhausted",
        )

    async def solve_file_token(self) -> SolveResult:
        """Consume one token from the FIFO token file."""
        t0 = time.monotonic()
        if not self._token_file.exists():
            return SolveResult(
                method=SolveMethod.FILE_TOKEN,
                elapsed_seconds=time.monotonic() - t0,
                error="token file does not exist",
            )

        try:
            lines = [
                l.strip()
                for l in self._token_file.read_text().splitlines()
                if l.strip().startswith("P1_")
            ]
        except OSError as exc:
            return SolveResult(
                method=SolveMethod.FILE_TOKEN,
                elapsed_seconds=time.monotonic() - t0,
                error=f"read error: {exc}",
            )

        if not lines:
            return SolveResult(
                method=SolveMethod.FILE_TOKEN,
                elapsed_seconds=time.monotonic() - t0,
                error="no tokens in file",
            )

        token = lines[0]
        # Remove consumed token (FIFO)
        remaining = lines[1:]
        self._token_file.write_text(
            "\n".join(remaining) + "\n" if remaining else ""
        )

        if self._verbose:
            self._log(
                f"[file_token] consumed ({len(token)} chars, "
                f"{len(remaining)} remaining)"
            )

        return SolveResult(
            token=token,
            method=SolveMethod.FILE_TOKEN,
            elapsed_seconds=time.monotonic() - t0,
            success=True,
        )

    async def solve_accessibility(self, *, max_wait: float = 30.0) -> SolveResult:
        """Solve via hCaptcha accessibility cookie (auto-pass, no visual challenge).

        If a valid accessibility cookie exists, injects it into BrowserOS,
        renders an invisible widget, and polls for the auto-pass token.
        """
        t0 = time.monotonic()
        try:
            from ._accessibility import AccessibilityCookieManager
        except ImportError:
            return SolveResult(
                method=SolveMethod.ACCESSIBILITY,
                elapsed_seconds=time.monotonic() - t0,
                error="accessibility module not available",
            )

        mgr = AccessibilityCookieManager(
            browseros_port=self._browseros_port,
            verbose=self._verbose,
        )

        if not mgr.is_cookie_valid():
            return SolveResult(
                method=SolveMethod.ACCESSIBILITY,
                elapsed_seconds=time.monotonic() - t0,
                error="no valid accessibility cookie",
            )

        tab_id = self._find_suno_tab()
        if tab_id is None:
            return SolveResult(
                method=SolveMethod.ACCESSIBILITY,
                elapsed_seconds=time.monotonic() - t0,
                error="no suno.com tab found in BrowserOS",
            )

        # Inject cookie into browser session
        injected = await mgr.inject_cookie()
        if not injected:
            return SolveResult(
                method=SolveMethod.ACCESSIBILITY,
                elapsed_seconds=time.monotonic() - t0,
                error="cookie injection failed",
            )

        if self._verbose:
            self._log("[accessibility] cookie injected, rendering invisible widget...")

        # Clean up stale widgets first
        self._cleanup_widgets(tab_id)
        await asyncio.sleep(0.3)

        # Render invisible widget + execute (accessibility cookie → auto-pass)
        self._js(tab_id, f"""(function(){{
            var c = document.createElement('div');
            c.id = 'hcap-accessibility';
            c.style.cssText = 'position:fixed;bottom:0;left:0;width:1px;height:1px;overflow:hidden;';
            document.body.appendChild(c);
            window.__captchaSolveToken = '';
            hcaptcha.render('hcap-accessibility', {{
                sitekey: '{HCAPTCHA_SITEKEY}',
                size: 'invisible',
                callback: function(t){{ window.__captchaSolveToken = t; }}
            }});
            hcaptcha.execute();
        }})()""")

        # Poll for auto-pass token
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            token_str = self._js(tab_id, """
                var t = window.__captchaSolveToken || '';
                try { var r = hcaptcha.getResponse() || ''; if (r.length > t.length) t = r; } catch(e){}
                t;
            """)
            token = re.search(r"P1_[A-Za-z0-9_\-\.]+", token_str)
            if token and len(token.group(0)) > 100:
                tok = token.group(0)
                if self._verbose:
                    self._log(f"[accessibility] auto-pass token ({len(tok)} chars)")
                return SolveResult(
                    token=tok,
                    method=SolveMethod.ACCESSIBILITY,
                    elapsed_seconds=time.monotonic() - t0,
                    success=True,
                )

        return SolveResult(
            method=SolveMethod.ACCESSIBILITY,
            elapsed_seconds=time.monotonic() - t0,
            error=f"accessibility auto-pass timed out after {max_wait}s",
        )

    async def solve_browseros_auto(self, *, max_wait: float = 30.0) -> SolveResult:
        """Render a FRESH hCaptcha widget in BrowserOS and wait for auto-solve.

        Cleans up ALL stale widgets/iframes first, ensures hcaptcha JS is loaded,
        then renders with size:'normal' and an explicit callback.
        """
        import httpx

        t0 = time.monotonic()

        tab_id = self._find_suno_tab()
        if tab_id is None:
            return SolveResult(
                method=SolveMethod.BROWSEROS_AUTO,
                elapsed_seconds=time.monotonic() - t0,
                error="no suno.com tab found in BrowserOS",
            )

        if self._verbose:
            self._log(f"[browseros_auto] tab={tab_id}, cleaning up + fresh widget")

        # Clean up ALL stale widgets before rendering
        self._cleanup_widgets(tab_id)
        await asyncio.sleep(0.5)

        # Check hcaptcha JS is loaded (navigate to suno.com if not)
        hcap_ready = self._js(tab_id, "typeof hcaptcha !== 'undefined'")
        if "true" not in str(hcap_ready).lower():
            if self._verbose:
                self._log("[browseros_auto] hcaptcha not loaded, navigating to suno.com...")
            try:
                self._mcp_call({
                    "name": "browser_navigate",
                    "arguments": {"tabId": tab_id, "url": "https://suno.com/create"},
                }, timeout=20.0)
            except Exception:
                pass
            await asyncio.sleep(6)

            hcap_ready = self._js(tab_id, "typeof hcaptcha !== 'undefined'")
            if "true" not in str(hcap_ready).lower():
                return SolveResult(
                    method=SolveMethod.BROWSEROS_AUTO,
                    elapsed_seconds=time.monotonic() - t0,
                    error="hcaptcha JS not available after navigation",
                )

        # Simulate mouse movement before captcha interaction
        await self._simulate_mouse_movement(tab_id)

        # Render FRESH widget with size:'normal' + explicit callback
        self._js(tab_id, f"""(function(){{
            var c = document.createElement('div');
            c.id = 'hcap-auto-solve';
            c.style.cssText = 'position:fixed;bottom:80px;left:50%;transform:translateX(-50%);z-index:99999;';
            document.body.appendChild(c);
            window.__hcap_auto = '';
            var wid = hcaptcha.render('hcap-auto-solve', {{
                sitekey: '{HCAPTCHA_SITEKEY}',
                size: 'normal',
                callback: function(t){{ window.__hcap_auto = t; }}
            }});
            hcaptcha.execute(wid, {{async: true}}).then(function(resp){{
                if (resp && resp.response) window.__hcap_auto = resp.response;
            }}).catch(function(e){{
                window.__hcap_auto = 'ERROR:' + e.message;
            }});
        }})()""")

        if self._verbose:
            self._log("[browseros_auto] fresh widget rendered, polling for solve...")

        # Poll for result
        deadline = time.monotonic() + max_wait
        poll_interval = 2.0
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)

            result_str = self._js(tab_id, """
                var auto = window.__hcap_auto || '';
                var resp = '';
                try { resp = hcaptcha.getResponse() || ''; } catch(e) {}
                var token = auto.length > 100 ? auto : (resp.length > 100 ? resp : '');
                token.length > 100 ? 'OK:' + token.length + ':' + token : 'waiting:' + auto.length + ':' + resp.length;
            """)

            if result_str.startswith("OK:"):
                parts = result_str.split(":", 2)
                token = parts[2] if len(parts) >= 3 else ""
                if self.validate_token(token):
                    self._js(tab_id, "window.__hcap_auto = '';")
                    if self._verbose:
                        self._log(f"[browseros_auto] solved ({len(token)} chars)")
                    return SolveResult(
                        token=token,
                        method=SolveMethod.BROWSEROS_AUTO,
                        elapsed_seconds=time.monotonic() - t0,
                        success=True,
                    )

        # Timeout fallback — try getResponse one more time
        fallback = self._js(tab_id, "try { hcaptcha.getResponse() || '' } catch(e) { '' }")
        m = re.search(r"P1_[A-Za-z0-9_\-\.]+", fallback)
        if m and len(m.group(0)) > 100:
            token = m.group(0)
            if self._verbose:
                self._log(f"[browseros_auto] fallback token ({len(token)} chars)")
            return SolveResult(
                token=token,
                method=SolveMethod.BROWSEROS_AUTO,
                elapsed_seconds=time.monotonic() - t0,
                success=True,
            )

        return SolveResult(
            method=SolveMethod.BROWSEROS_AUTO,
            elapsed_seconds=time.monotonic() - t0,
            error=f"timed out after {max_wait}s",
        )

    async def solve_browseros_vision(self, *, timeout: float = 1800.0) -> SolveResult:
        """Solve hCaptcha via BrowserOS screenshots + claudex/claude vision classification.

        Uses BrowserOS MCP to render widget, take screenshots, and click cells.
        Vision classification chain: claudex → claude → NVIDIA NIM → YOLO.
        """
        t0 = time.monotonic()
        try:
            from ._browseros_solver import BrowserOSVisionSolver
        except ImportError as exc:
            return SolveResult(
                method=SolveMethod.BROWSEROS_VISION,
                elapsed_seconds=time.monotonic() - t0,
                error=f"browseros_solver import failed: {exc}",
            )

        tab_id = self._find_suno_tab()
        if tab_id is None:
            return SolveResult(
                method=SolveMethod.BROWSEROS_VISION,
                elapsed_seconds=time.monotonic() - t0,
                error="no suno.com tab found in BrowserOS",
            )

        solver = BrowserOSVisionSolver(
            tab_id=tab_id,
            mcp_caller=self._mcp_call,
            js_executor=self._js,
            cleanup_fn=self._cleanup_widgets,
            browseros_port=self._browseros_port,
            verbose=self._verbose,
        )

        try:
            token = await solver.solve(timeout=timeout)
        except Exception as exc:
            return SolveResult(
                method=SolveMethod.BROWSEROS_VISION,
                elapsed_seconds=time.monotonic() - t0,
                error=f"browseros vision solve error: {exc}",
            )

        if token and self.validate_token(token):
            if self._verbose:
                self._log(f"[browseros_vision] solved ({len(token)} chars)")
            return SolveResult(
                token=token,
                method=SolveMethod.BROWSEROS_VISION,
                elapsed_seconds=time.monotonic() - t0,
                success=True,
            )

        return SolveResult(
            method=SolveMethod.BROWSEROS_VISION,
            elapsed_seconds=time.monotonic() - t0,
            error="BrowserOS vision solver did not produce a valid token",
        )

    async def solve_yolo_local(self, *, max_wait: float = 60.0) -> SolveResult:
        """Solve hCaptcha using local YOLO model (optional dependency)."""
        t0 = time.monotonic()
        try:
            from ._yolo_solver import YoloCaptchaSolver
        except ImportError:
            return SolveResult(
                method=SolveMethod.YOLO_LOCAL,
                elapsed_seconds=time.monotonic() - t0,
                error="ultralytics not installed (pip install suno-wrapper[yolo])",
            )

        tab_id = self._find_suno_tab()
        if tab_id is None:
            return SolveResult(
                method=SolveMethod.YOLO_LOCAL,
                elapsed_seconds=time.monotonic() - t0,
                error="no suno.com tab found in BrowserOS",
            )

        yolo = YoloCaptchaSolver(
            tab_id=tab_id,
            mcp_caller=self._mcp_call,
            js_executor=self._js,
        )
        token = await yolo.solve(max_wait=max_wait)

        if token and self.validate_token(token):
            if self._verbose:
                self._log(f"[yolo_local] solved ({len(token)} chars)")
            return SolveResult(
                token=token,
                method=SolveMethod.YOLO_LOCAL,
                elapsed_seconds=time.monotonic() - t0,
                success=True,
            )

        return SolveResult(
            method=SolveMethod.YOLO_LOCAL,
            elapsed_seconds=time.monotonic() - t0,
            error="YOLO solver did not produce a valid token",
        )

    async def solve_capsolver(self, *, timeout: float = 120.0) -> SolveResult:
        """Solve hCaptcha via capsolver.com API."""
        import httpx

        t0 = time.monotonic()

        if not self._capsolver_api_key:
            return SolveResult(
                method=SolveMethod.CAPSOLVER,
                elapsed_seconds=time.monotonic() - t0,
                error="no CAPSOLVER_API_KEY configured",
            )

        if self._verbose:
            self._log("[capsolver] solving via capsolver.com...")

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                # Create task
                r = await client.post(
                    "https://api.capsolver.com/createTask",
                    json={
                        "clientKey": self._capsolver_api_key,
                        "task": {
                            "type": "HCaptchaTaskProxyLess",
                            "websiteURL": HCAPTCHA_PAGE_URL,
                            "websiteKey": HCAPTCHA_SITEKEY,
                        },
                    },
                )
                data = r.json()
                task_id = data.get("taskId")
                if not task_id:
                    return SolveResult(
                        method=SolveMethod.CAPSOLVER,
                        elapsed_seconds=time.monotonic() - t0,
                        error=f"createTask failed: {data}",
                    )

                # Poll for result
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    await asyncio.sleep(3)
                    r = await client.post(
                        "https://api.capsolver.com/getTaskResult",
                        json={
                            "clientKey": self._capsolver_api_key,
                            "taskId": task_id,
                        },
                    )
                    result = r.json()
                    status = result.get("status", "")

                    if status == "ready":
                        token = result.get("solution", {}).get(
                            "gRecaptchaResponse", ""
                        )
                        if self.validate_token(token):
                            if self._verbose:
                                self._log(
                                    f"[capsolver] solved ({len(token)} chars)"
                                )
                            return SolveResult(
                                token=token,
                                method=SolveMethod.CAPSOLVER,
                                elapsed_seconds=time.monotonic() - t0,
                                success=True,
                            )
                        return SolveResult(
                            method=SolveMethod.CAPSOLVER,
                            elapsed_seconds=time.monotonic() - t0,
                            error=f"unexpected solution keys: {list(result.get('solution', {}).keys())}",
                        )
                    elif status == "failed":
                        return SolveResult(
                            method=SolveMethod.CAPSOLVER,
                            elapsed_seconds=time.monotonic() - t0,
                            error=f"task failed: {result.get('errorDescription', '')}",
                        )

                return SolveResult(
                    method=SolveMethod.CAPSOLVER,
                    elapsed_seconds=time.monotonic() - t0,
                    error=f"timeout ({timeout}s)",
                )

        except Exception as exc:
            return SolveResult(
                method=SolveMethod.CAPSOLVER,
                elapsed_seconds=time.monotonic() - t0,
                error=f"capsolver error: {exc}",
            )

    async def solve_vnc_manual(self, *, timeout: float = 600.0) -> SolveResult:
        """Trigger VNC stack and wait for manual captcha solve.

        Polls /tmp/suno_generate_token.txt for file changes.
        """
        t0 = time.monotonic()

        # Get the current token (if any) to detect changes
        old_token = ""
        if GENERATE_TOKEN_FILE.exists():
            old_token = GENERATE_TOKEN_FILE.read_text().strip()

        # Try to trigger VNC stack (best-effort)
        try:
            from suno_auth import trigger_captcha_vnc

            trigger_captcha_vnc(
                "captcha_solver: automated methods exhausted, manual solve needed",
                alert_key="captcha_solver_vnc",
                throttle_minutes=10,
            )
        except ImportError:
            if self._verbose:
                self._log("[vnc_manual] suno_auth.trigger_captcha_vnc not available")
        except Exception as exc:
            if self._verbose:
                self._log(f"[vnc_manual] trigger_captcha_vnc error: {exc}")

        if self._verbose:
            self._log(f"[vnc_manual] waiting up to {timeout}s for manual solve...")

        deadline = time.monotonic() + timeout
        poll_interval = 10.0
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)

            if GENERATE_TOKEN_FILE.exists():
                new_token = GENERATE_TOKEN_FILE.read_text().strip()
                if new_token and new_token != old_token and self.validate_token(new_token):
                    if self._verbose:
                        self._log(f"[vnc_manual] manual solve detected ({len(new_token)} chars)")
                    return SolveResult(
                        token=new_token,
                        method=SolveMethod.VNC_MANUAL,
                        elapsed_seconds=time.monotonic() - t0,
                        success=True,
                    )

        return SolveResult(
            method=SolveMethod.VNC_MANUAL,
            elapsed_seconds=time.monotonic() - t0,
            error=f"no manual solve within {timeout}s",
        )

    async def solve_cdp_visual(self, *, timeout: float = 180.0) -> SolveResult:
        """Solve hCaptcha via CDP + xdotool with AI vision cell classification.

        Uses Chrome DevTools Protocol to access the hCaptcha iframe,
        crops cells from DPR-aware screenshots, classifies via NVIDIA NIM
        vision models (with MiniMax text + YOLO fallback), and clicks
        using xdotool with human-like bezier mouse curves.

        Requires: websockets, Pillow, xdotool, Xvfb.
        Optional: ultralytics (for YOLO classification fallback).
        """
        t0 = time.monotonic()
        try:
            from ._cdp_solver import CdpCaptchaSolver
        except ImportError as exc:
            return SolveResult(
                method=SolveMethod.CDP_VISUAL,
                elapsed_seconds=time.monotonic() - t0,
                error=f"cdp_solver import failed: {exc}",
            )

        cdp_solver = CdpCaptchaSolver(
            cdp_port=self._cdp_port,
            verbose=self._verbose,
            vision_api_key=(
                os.environ.get("NVIDIA_KIMI_API_KEY", "")
                or os.environ.get("NVIDIA_MINIMAX_API_KEY", "")
            ),
        )
        try:
            token = await cdp_solver.solve(timeout=timeout)
        except Exception as exc:
            return SolveResult(
                method=SolveMethod.CDP_VISUAL,
                elapsed_seconds=time.monotonic() - t0,
                error=f"cdp solve error: {exc}",
            )

        if token and self.validate_token(token):
            if self._verbose:
                self._log(f"[cdp_visual] solved ({len(token)} chars)")
            return SolveResult(
                token=token,
                method=SolveMethod.CDP_VISUAL,
                elapsed_seconds=time.monotonic() - t0,
                success=True,
            )

        return SolveResult(
            method=SolveMethod.CDP_VISUAL,
            elapsed_seconds=time.monotonic() - t0,
            error="CDP visual solver did not produce a valid token",
        )

    def save_token(self, token: str) -> None:
        """Save token to standard locations (/tmp files + ~/.env.suno)."""
        from .env_util import save_token as _save_token

        _save_token(token)

    def validate_token(self, token: str) -> bool:
        """Check if a token looks like a valid hCaptcha P1_ token."""
        if not token or not isinstance(token, str):
            return False
        token = token.strip()
        return token.startswith("P1_") and len(token) > 100

    # ── Widget cleanup ─────────────────────────────────────────────

    def _cleanup_widgets(self, tab_id: int) -> None:
        """Remove ALL hcaptcha containers, iframes, and global state.

        Must be called before rendering a new widget to avoid stacked
        widgets interfering with callbacks and rate-limiting issues.
        """
        self._js(tab_id, """(function(){
            // Remove known auto-render containers
            ['cdp-hcaptcha','hcap-auto-solve','hcap-accessibility','hcap-bvs-solve'].forEach(function(id){
                var el = document.getElementById(id);
                if (el) el.remove();
            });

            // Remove ALL hcaptcha-related containers and iframes
            document.querySelectorAll('[data-hcaptcha-widget-id]').forEach(function(el){ el.remove(); });
            document.querySelectorAll('iframe[src*="hcaptcha"]').forEach(function(el){ el.remove(); });
            document.querySelectorAll('.h-captcha').forEach(function(el){ el.remove(); });

            // Clear global state
            try { delete window.__hcap_auto; } catch(e){}
            try { delete window.__captchaSolveToken; } catch(e){}
            try { delete window._captchaToken; } catch(e){}
            try { delete window.__bvs_token; } catch(e){}

            // Reset hcaptcha internal state if available
            try {
                if (typeof hcaptcha !== 'undefined' && hcaptcha.reset) {
                    hcaptcha.reset();
                }
            } catch(e){}
        })()""", timeout=8.0)

        if self._verbose:
            self._log("[cleanup] removed all hcaptcha widgets + global state")

    # ── BrowserOS MCP helpers ────────────────────────────────────────

    def _mcp_call(self, params: dict, timeout: float = 15.0) -> dict:
        """Call BrowserOS MCP endpoint (sync)."""
        import httpx

        r = httpx.post(
            f"http://127.0.0.1:{self._browseros_port}/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": params,
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            timeout=timeout,
        )
        return r.json()

    def _js(self, tab_id: int, code: str, timeout: float = 15.0) -> str:
        """Execute JS in a BrowserOS tab and return the text result."""
        resp = self._mcp_call(
            {
                "name": "browser_execute_javascript",
                "arguments": {"tabId": tab_id, "code": code},
            },
            timeout=timeout,
        )
        result = resp.get("result", "")
        if isinstance(result, dict):
            content = result.get("content", [])
            if content and isinstance(content, list):
                text = content[0].get("text", str(result))
                m = re.search(r"^Result:\s*(.+)", text, re.MULTILINE | re.DOTALL)
                if m:
                    return m.group(1).strip()
                return text
        return str(result)

    def _find_suno_tab(self) -> int | None:
        """Find the suno.com tab ID in BrowserOS."""
        try:
            resp = self._mcp_call({"name": "browser_list_tabs", "arguments": {}})
            text = json.dumps(resp)
            m = re.search(r'"id"\s*:\s*(\d+).*?"url"\s*:\s*"https?://suno\.com', text)
            return int(m.group(1)) if m else None
        except Exception:
            return None

    async def _simulate_mouse_movement(self, tab_id: int) -> None:
        """Simulate random mouse paths before captcha interaction."""
        import random

        # Generate a simple random path of mouse movements
        moves = random.randint(3, 7)
        js_parts = []
        for _ in range(moves):
            x = random.randint(100, 900)
            y = random.randint(100, 600)
            js_parts.append(
                f"document.dispatchEvent(new MouseEvent('mousemove', "
                f"{{clientX: {x}, clientY: {y}, bubbles: true}}));"
            )

        code = "\n".join(js_parts)
        try:
            self._js(tab_id, code)
        except Exception:
            pass  # best-effort

        # Small human-like delay after mouse movement
        await asyncio.sleep(random.uniform(0.3, 0.8))

    # ── Internal dispatch ────────────────────────────────────────────

    async def _dispatch(self, method: SolveMethod, remaining: float) -> SolveResult:
        """Dispatch to the correct solver method."""
        if method == SolveMethod.FILE_TOKEN:
            return await self.solve_file_token()
        elif method == SolveMethod.ACCESSIBILITY:
            wait = min(remaining, 30.0)
            return await self.solve_accessibility(max_wait=wait)
        elif method == SolveMethod.BROWSEROS_AUTO:
            wait = min(remaining, 30.0)
            return await self.solve_browseros_auto(max_wait=wait)
        elif method == SolveMethod.BROWSEROS_VISION:
            wait = min(remaining, 1800.0)
            return await self.solve_browseros_vision(timeout=wait)
        elif method == SolveMethod.YOLO_LOCAL:
            wait = min(remaining, 60.0)
            return await self.solve_yolo_local(max_wait=wait)
        elif method == SolveMethod.CAPSOLVER:
            wait = min(remaining, 120.0)
            return await self.solve_capsolver(timeout=wait)
        elif method == SolveMethod.CDP_VISUAL:
            wait = min(remaining, 180.0)
            return await self.solve_cdp_visual(timeout=wait)
        elif method == SolveMethod.VNC_MANUAL:
            wait = min(remaining, 600.0)
            return await self.solve_vnc_manual(timeout=wait)
        else:
            return SolveResult(error=f"unknown method: {method}")

    def _log(self, msg: str) -> None:
        if self._verbose:
            print(f"  [captcha_solver] {msg}")
