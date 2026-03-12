"""BrowserOS vision captcha solver.

Uses BrowserOS MCP for JS execution/navigation and CDP WebSocket for
screenshots (Page.captureScreenshot) and mouse clicks (Input.dispatchMouseEvent).

Classification chain (first success wins):
1. NVIDIA NIM Gemma 3 27B (multimodal, ~2s response) — primary
2. claude CLI (Anthropic Claude, multimodal) — fallback
3. YOLO local (COCO classes only) — last resort

Click mechanism: CDP Input.dispatchMouseEvent via WebSocket.
BrowserOS browser_click_coordinates does NOT register on cross-origin hCaptcha
iframes. browser_click_element corrupts the challenge. Only CDP dispatchMouseEvent
at CSS viewport coordinates works reliably.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import os
import re
import shutil
import time
import urllib.request
from pathlib import Path
from typing import Any

# Fallback grid coordinates (for tests and non-CDP environments).
# Real solving uses dynamic positions computed from the iframe bounding rect.
GRID_COORDS: dict[tuple[int, int], tuple[int, int]] = {
    (0, 0): (192, 225), (0, 1): (295, 225), (0, 2): (400, 225),
    (1, 0): (192, 335), (1, 1): (295, 335), (1, 2): (400, 335),
    (2, 0): (192, 440), (2, 1): (295, 440), (2, 2): (400, 440),
}
VERIFY_BUTTON: tuple[int, int] = (427, 530)

HCAPTCHA_SITEKEY = "d65453de-3f1a-4aac-9366-a0f06e52b2ce"
SCREENSHOT_PATH = "/tmp/bvs_challenge.png"
SCREENSHOT_WIDTH = 630
SCREENSHOT_HEIGHT = 626

CLAUDEX_BIN = "/home/codex/.local/bin/claudex"


def _build_classification_prompt(
    image_path: str = SCREENSHOT_PATH,
    challenge_text: str = "",
) -> str:
    """Build the vision classification prompt for claudex/claude.

    If challenge_text is provided (extracted from iframe DOM), it's included
    directly so the model knows exactly what to look for without having to
    read tiny text from the screenshot.
    """
    challenge_hint = ""
    if challenge_text:
        challenge_hint = (
            f"\nThe challenge prompt says: \"{challenge_text}\"\n"
            "Select ONLY cells that clearly match this specific prompt.\n"
        )

    return (
        f"Read the image at {image_path}. This is a screenshot of an hCaptcha visual challenge.\n"
        "\n"
        "The image shows a 3x3 grid of cells. Each cell contains a photo.\n"
        f"{challenge_hint}"
        "\n"
        "Grid layout (row, col):\n"
        "  (0,0) (0,1) (0,2)\n"
        "  (1,0) (1,1) (1,2)\n"
        "  (2,0) (2,1) (2,2)\n"
        "\n"
        "IMPORTANT RULES:\n"
        "- Be conservative: only select cells where the object is clearly and unambiguously present.\n"
        "- If unsure about a cell, do NOT select it. False positives cause failure.\n"
        "- Usually 2-4 cells match. Selecting 0 or all 9 is almost always wrong.\n"
        "- Ignore backgrounds, textures, and partial/ambiguous matches.\n"
        "\n"
        "Return ONLY a JSON array of [row, col] pairs for matching cells.\n"
        "Example: [[0,1],[1,2],[2,0]]\n"
        "If no cells clearly match, return []\n"
        "Return raw JSON only, no explanation."
    )


def _parse_cell_response(text: str) -> list[tuple[int, int]]:
    """Parse a JSON array of [row, col] from model response.

    Handles raw JSON, markdown-wrapped code blocks, and noisy text.
    """
    text = text.strip()
    if not text:
        return []

    # Strip markdown code fences
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()

    # Try direct JSON parse
    try:
        arr = json.loads(text)
        if isinstance(arr, list):
            return [
                (int(cell[0]), int(cell[1]))
                for cell in arr
                if isinstance(cell, (list, tuple)) and len(cell) >= 2
            ]
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    # Fallback: find array pattern in text
    m = re.search(r"\[[\s\[\]\d,]+\]", text)
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list):
                return [
                    (int(cell[0]), int(cell[1]))
                    for cell in arr
                    if isinstance(cell, (list, tuple)) and len(cell) >= 2
                ]
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    # Fallback: extract (row, col) pairs marked with ✓ from reasoning text
    # Handles Kimi K2.5 reasoning output like "- (0,1): Dolphin ✓"
    cells = []
    for line in text.split("\n"):
        line_stripped = line.strip()
        coord_match = re.search(r"\((\d),\s*(\d)\)", line_stripped)
        if coord_match:
            row, col = int(coord_match.group(1)), int(coord_match.group(2))
            if 0 <= row <= 2 and 0 <= col <= 2:
                if "✓" in line_stripped or "✅" in line_stripped:
                    cells.append((row, col))
    if cells:
        return cells

    return []


class BrowserOSVisionSolver:
    """Solve hCaptcha via BrowserOS screenshots + claudex/claude vision classification."""

    def __init__(
        self,
        *,
        tab_id: int,
        mcp_caller: Any,
        js_executor: Any,
        cleanup_fn: Any,
        browseros_port: int = 9200,
        verbose: bool = True,
    ) -> None:
        self._tab_id = tab_id
        self._mcp_call = mcp_caller
        self._js = js_executor
        self._cleanup = cleanup_fn
        self._browseros_port = browseros_port
        self._verbose = verbose

        # Track claudex failures across rounds to know when to escalate
        self._claudex_failures = 0
        # Track rounds where claudex returned cells but hCaptcha rejected them
        self._rounds_without_token = 0

        # Cache CDP WebSocket URL (discovered once, reused for screenshots + clicks)
        self._cdp_ws_url: str | None = None
        # Challenge prompt text extracted from iframe DOM
        self._challenge_text: str = ""

    async def solve(self, *, max_rounds: int = 15, timeout: float = 1800.0) -> str | None:
        """Full solve flow: render widget → screenshot → classify → click → poll token.

        Returns the P1_ token string on success, None on failure.
        """
        deadline = time.monotonic() + timeout

        # 1. Clean up stale widgets
        self._cleanup(self._tab_id)
        await asyncio.sleep(0.5)

        # 2. Verify hcaptcha JS loaded — retry with navigation + injection
        hcap_ready = self._js(self._tab_id, "typeof hcaptcha !== 'undefined'")
        if "true" not in str(hcap_ready).lower():
            self._log("hcaptcha not loaded, navigating to suno.com/create...")
            try:
                self._mcp_call({
                    "name": "browser_navigate",
                    "arguments": {"tabId": self._tab_id, "url": "https://suno.com/create"},
                }, timeout=20.0)
            except Exception:
                pass
            # Wait longer for SPA to fully load (hCaptcha loads after React hydration)
            for wait_s in range(12):
                await asyncio.sleep(1)
                hcap_ready = self._js(self._tab_id, "typeof hcaptcha !== 'undefined'")
                if "true" in str(hcap_ready).lower():
                    self._log(f"hcaptcha loaded after {wait_s + 1}s")
                    break

            if "true" not in str(hcap_ready).lower():
                # Inject hCaptcha script explicitly — Suno may not load it globally
                self._log("injecting hCaptcha script manually...")
                self._js(self._tab_id, """(function(){
                    var s = document.createElement('script');
                    s.src = 'https://js.hcaptcha.com/1/api.js?render=explicit&recaptchacompat=off';
                    s.async = true;
                    document.head.appendChild(s);
                })()""")
                for wait_s in range(8):
                    await asyncio.sleep(1)
                    hcap_ready = self._js(self._tab_id, "typeof hcaptcha !== 'undefined'")
                    if "true" in str(hcap_ready).lower():
                        self._log(f"hcaptcha loaded after injection ({wait_s + 1}s)")
                        break

                if "true" not in str(hcap_ready).lower():
                    self._log("hcaptcha JS not available after injection")
                    return None

        # 3. Render fresh widget
        self._js(self._tab_id, f"""(function(){{
            var c = document.createElement('div');
            c.id = 'hcap-bvs-solve';
            c.style.cssText = 'position:fixed;bottom:80px;left:50%;transform:translateX(-50%);z-index:99999;';
            document.body.appendChild(c);
            window.__bvs_token = '';
            var wid = hcaptcha.render('hcap-bvs-solve', {{
                sitekey: '{HCAPTCHA_SITEKEY}',
                size: 'normal',
                callback: function(t){{ window.__bvs_token = t; }}
            }});
            hcaptcha.execute(wid, {{async: true}}).then(function(resp){{
                if (resp && resp.response) window.__bvs_token = resp.response;
            }}).catch(function(e){{}});
        }})()""")

        self._log("widget rendered, waiting for challenge iframe...")
        await asyncio.sleep(3)

        # 4. Wait for challenge iframe
        iframe_found = await self._wait_for_iframe(deadline)

        if not iframe_found:
            # Check if auto-pass token was granted (no visual challenge needed)
            token = self._poll_token_sync()
            if token:
                self._log(f"auto-pass token ({len(token)} chars)")
                return token
            self._log("no challenge iframe appeared")
            self._cleanup_container()
            return None

        # Wait for grid images to load (they appear as CSS background-images)
        self._log("waiting for grid images to load...")
        await asyncio.sleep(5)

        # 5. Extract challenge prompt text from iframe DOM
        self._challenge_text = self._extract_challenge_text()
        if self._challenge_text:
            self._log(f"challenge prompt: \"{self._challenge_text}\"")

        # 6. Solve loop
        for round_num in range(1, max_rounds + 1):
            if time.monotonic() >= deadline:
                self._log("timeout reached")
                break

            self._log(f"--- round {round_num}/{max_rounds} ---")

            # Re-extract challenge text (may change between rounds/pages)
            if round_num > 1:
                new_text = self._extract_challenge_text()
                if new_text and new_text != self._challenge_text:
                    self._challenge_text = new_text
                    self._log(f"challenge changed: \"{self._challenge_text}\"")

            # Read grid cell positions dynamically from the iframe bounding rect
            grid_info = self._read_grid_positions()
            if not grid_info:
                self._log("could not read grid positions, using fallback coords")

            # Take screenshot (cropped to iframe for better vision classification)
            # Retry up to 3 times with CDP WS URL reset on failure
            iframe_rect = grid_info.get("iframe") if grid_info else None
            screenshot_ok = False
            for _ss_attempt in range(3):
                screenshot_ok = self._take_screenshot(crop_to_iframe=iframe_rect)
                if screenshot_ok:
                    break
                self._log(f"screenshot attempt {_ss_attempt + 1}/3 failed, resetting CDP WS cache...")
                self._cdp_ws_url = None  # force rediscovery
                await asyncio.sleep(2)
            if not screenshot_ok:
                self._log("screenshot failed after 3 retries")
                break

            # Classify cells via vision chain
            cells = await self._classify_with_vision()
            if not cells:
                self._log("vision chain returned no matching cells, clicking skip")
                # Click skip to advance past this page
                verify_pos = grid_info.get("verify") if grid_info else None
                if verify_pos:
                    self._click_at(verify_pos[0], verify_pos[1])
                else:
                    self._click_at(*VERIFY_BUTTON)
                await asyncio.sleep(2)
                # Check for token (might get auto-pass)
                token = self._poll_token_sync()
                if token:
                    self._log(f"token after skip ({len(token)} chars)")
                    self._cleanup_container()
                    return token
                continue

            self._log(f"clicking {len(cells)} cells: {cells}")

            # Click matching cells using dynamic coords or fallback
            for row, col in cells:
                coord = None
                if grid_info:
                    coord = grid_info.get("cells", {}).get(f"{row},{col}")
                if not coord:
                    coord = GRID_COORDS.get((row, col))
                if coord:
                    self._click_at(coord[0], coord[1])
                    await asyncio.sleep(0.3)

            # Click verify/next button
            await asyncio.sleep(0.5)
            verify_pos = grid_info.get("verify") if grid_info else None
            if verify_pos:
                self._click_at(verify_pos[0], verify_pos[1])
            else:
                self._click_at(*VERIFY_BUTTON)
            self._log("clicked verify/next")

            # Poll for token (wait for server-side verification)
            await asyncio.sleep(3)
            token = self._poll_token_sync()
            if token:
                self._log(f"solved! token ({len(token)} chars)")
                self._cleanup_container()
                return token

            # Check if iframe still visible for next round.
            # After clicking verify, hCaptcha briefly hides the iframe while
            # loading a new challenge page. Wait up to 8s for it to reappear.
            iframe_visible = self._check_iframe_visible()
            if not iframe_visible:
                # Wait for iframe to reappear (new challenge page loading)
                self._log("iframe not visible, waiting for reappear or token...")
                reappeared = False
                for wait_i in range(8):
                    await asyncio.sleep(1)
                    token = self._poll_token_sync()
                    if token:
                        self._log(f"delayed token ({len(token)} chars)")
                        self._cleanup_container()
                        return token
                    if self._check_iframe_visible():
                        self._log(f"iframe reappeared after {wait_i + 1}s")
                        reappeared = True
                        # Wait for new images to load
                        await asyncio.sleep(3)
                        break
                if not reappeared:
                    # Challenge was dismissed (wrong answer or hCaptcha reset).
                    # Re-execute the widget to trigger a fresh challenge.
                    self._log("iframe dismissed, re-executing hcaptcha to get new challenge...")
                    self._js(self._tab_id, """(function(){
                        try {
                            // Find and re-execute the widget
                            var wid = document.querySelector('#hcap-bvs-solve iframe');
                            if (wid) {
                                // Widget exists, try execute on it
                                var widgetIds = Object.keys(hcaptcha._state || {});
                                for (var wid of widgetIds) {
                                    hcaptcha.execute(wid, {async: true}).catch(function(e){});
                                }
                            } else {
                                // Widget container gone, re-render
                                var c = document.getElementById('hcap-bvs-solve');
                                if (!c) {
                                    c = document.createElement('div');
                                    c.id = 'hcap-bvs-solve';
                                    c.style.cssText = 'position:fixed;bottom:80px;left:50%;transform:translateX(-50%);z-index:99999;';
                                    document.body.appendChild(c);
                                }
                                c.innerHTML = '';
                                window.__bvs_token = '';
                                var nw = hcaptcha.render('hcap-bvs-solve', {
                                    sitekey: '%s',
                                    size: 'normal',
                                    callback: function(t){ window.__bvs_token = t; }
                                });
                                hcaptcha.execute(nw, {async: true}).then(function(resp){
                                    if (resp && resp.response) window.__bvs_token = resp.response;
                                }).catch(function(e){});
                            }
                        } catch(e) {}
                    })()""" % HCAPTCHA_SITEKEY)
                    await asyncio.sleep(5)
                    # Wait for new challenge iframe
                    new_iframe = await self._wait_for_iframe(time.monotonic() + 15)
                    if new_iframe:
                        self._log("new challenge appeared after re-execute")
                        await asyncio.sleep(3)  # wait for images to load
                        continue
                    # Check for auto-pass token
                    token = self._poll_token_sync()
                    if token:
                        self._log(f"auto-pass after re-execute ({len(token)} chars)")
                        self._cleanup_container()
                        return token
                    self._log("no new challenge after re-execute")
                    break

            # Still visible — either "Please try again" or multi-page challenge.
            # Track rounds without token to escalate vision model tier faster
            self._rounds_without_token += 1
            # Wait for new images to load before next round.
            self._log("no token yet, waiting for next challenge page...")
            await asyncio.sleep(4)

        self._cleanup_container()
        return None

    # ── Vision classification chain ──────────────────────────────────

    async def _classify_with_vision(self) -> list[tuple[int, int]]:
        """Run the vision classification chain on the current screenshot.

        Chain order (first success wins):
        1. NVIDIA NIM Gemma 3 27B (multimodal, ~2s response, returns clean JSON)
        2. claude CLI (Anthropic Claude — multimodal, reads images natively)
        3. YOLO local (COCO object detection — last resort)
        """
        prompt = _build_classification_prompt(SCREENSHOT_PATH, self._challenge_text)

        # Tier 1: NVIDIA NIM Gemma 3 27B (multimodal via base64 — fast, 2s)
        nvidia_key = (
            os.environ.get("NVIDIA_KIMI_API_KEY", "").strip()
            or os.environ.get("NVIDIA_MINIMAX_API_KEY", "").strip()
            or os.environ.get("KIMI_API_KEY", "").strip()
        )
        if nvidia_key:
            self._log("tier 1: NVIDIA NIM Gemma 3 27B")
            result = await self._classify_nvidia_nim(nvidia_key)
            if result:
                return result
            self._log("NVIDIA NIM returned no cells")

        # Tier 2: claude CLI (Anthropic multimodal — can read image files)
        claude_bin = shutil.which("claude")
        if claude_bin:
            self._log("tier 2: claude CLI (Anthropic multimodal)")
            cmd = [claude_bin, "-p", prompt, "--output-format", "text", "--max-turns", "1"]
            result = await self._run_cli_classifier(cmd, timeout=45)
            if result:
                return result
            self._log("claude CLI returned no cells")

        # Tier 3: YOLO local (COCO object detection)
        result = self._classify_yolo()
        if result:
            self._log(f"tier 4: YOLO classified {len(result)} cells")
            return result

        return []

    async def _run_cli_classifier(
        self, cmd: list[str], timeout: int = 60,
    ) -> list[tuple[int, int]]:
        """Run a CLI tool (claudex or claude) as subprocess and parse cell output."""
        try:
            # Unset CLAUDECODE to allow nested claude sessions
            env = os.environ.copy()
            env.pop("CLAUDECODE", None)
            env.pop("CLAUDE_CODE_ENTRYPOINT", None)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
            output = stdout.decode(errors="replace").strip()
            if self._verbose and output:
                self._log(f"CLI output: {output[:200]}")

            cells = _parse_cell_response(output)
            if cells:
                return cells

        except asyncio.TimeoutError:
            self._log(f"CLI subprocess timed out ({timeout}s)")
            try:
                proc.kill()  # type: ignore[possibly-undefined]
            except Exception:
                pass
        except FileNotFoundError:
            self._log("CLI binary not found")
        except Exception as exc:
            self._log(f"CLI subprocess error: {exc}")

        return []

    async def _classify_nvidia_nim(self, api_key: str) -> list[tuple[int, int]]:
        """Classify using NVIDIA NIM Gemma 3 27B (fast multimodal, ~2s)."""
        try:
            import base64
            import httpx

            img_path = Path(SCREENSHOT_PATH)
            if not img_path.exists():
                return []

            img_b64 = base64.b64encode(img_path.read_bytes()).decode()
            challenge_hint = ""
            if self._challenge_text:
                challenge_hint = f'\nThe challenge prompt says: "{self._challenge_text}"\nSelect ONLY cells that clearly match this prompt.\n'

            prompt_text = (
                "You are solving a visual captcha challenge.\n"
                "The image shows an hCaptcha with a 3x3 grid of cells.\n"
                "Read the challenge prompt text at the top of the image carefully.\n"
                f"{challenge_hint}\n"
                "Grid layout (row, col):\n"
                "  (0,0) (0,1) (0,2)\n"
                "  (1,0) (1,1) (1,2)\n"
                "  (2,0) (2,1) (2,2)\n\n"
                "CRITICAL RULES:\n"
                "- Read the challenge prompt from the image FIRST\n"
                "- Be VERY selective — typically only 2-4 cells match\n"
                "- Do NOT select all cells. Most cells do NOT match.\n"
                "- Only select cells where the object CLEARLY matches the prompt\n"
                "- If unsure, do NOT select the cell\n\n"
                "Return ONLY a JSON array of [row, col] pairs, e.g. [[0,1],[1,2]]\n"
                "If no cells match, return []\n"
                "Return raw JSON only, no explanation."
            )

            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
                r = await client.post(
                    "https://integrate.api.nvidia.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "google/gemma-3-27b-it",
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt_text},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                                },
                            ],
                        }],
                        "max_tokens": 300,
                        "temperature": 0.1,
                    },
                )
                data = r.json()
                if r.status_code != 200:
                    self._log(f"NVIDIA NIM error {r.status_code}: {str(data)[:200]}")
                    return []

                msg = data.get("choices", [{}])[0].get("message", {})
                text = msg.get("content", "") or ""
                # Kimi K2.5 reasoning model may put response in reasoning field
                if not text.strip():
                    reasoning = msg.get("reasoning", "") or ""
                    if reasoning:
                        text = reasoning
                if text:
                    self._log(f"NVIDIA NIM response: {text.strip()[:120]}")
                    return _parse_cell_response(text)

        except Exception as exc:
            self._log(f"NVIDIA NIM exception: {type(exc).__name__}: {exc}")

        return []

    def _classify_yolo(self) -> list[tuple[int, int]]:
        """Classify cells using local YOLO (COCO classes only)."""
        try:
            from PIL import Image
            from ultralytics import YOLO
        except ImportError:
            return []

        img_path = Path(SCREENSHOT_PATH)
        if not img_path.exists():
            return []

        try:
            model = YOLO("yolov8n.pt")
            img = Image.open(img_path)

            cells = []
            for (row, col), (cx, cy) in GRID_COORDS.items():
                # Crop ~100x100 region around each cell center
                x1, y1 = cx - 50, cy - 50
                x2, y2 = cx + 50, cy + 50
                cell_img = img.crop((max(0, x1), max(0, y1), x2, y2))
                results = model.predict(cell_img, conf=0.35, verbose=False)
                if results and len(results[0].boxes) > 0:
                    cells.append((row, col))

            return cells
        except Exception as exc:
            self._log(f"YOLO error: {exc}")
            return []

    # ── BrowserOS interaction helpers ────────────────────────────────

    def _extract_challenge_text(self) -> str:
        """Extract the challenge prompt text from the hCaptcha iframe DOM.

        The challenge iframe contains a prompt like "Please click each image
        containing a basketball" or "Select all images with a bus". This text
        is critical for accurate classification.

        Uses CDP's Page.createIsolatedWorld to execute JS inside the iframe
        (cross-origin), or falls back to reading aria labels / title from
        the main page.
        """
        # Method 1: Try reading from iframe's aria labels or task prompt elements.
        # IMPORTANT: The hCaptcha *checkbox* iframe has title="Widget containing
        # checkbox..." — we must NOT return that. The *challenge* iframe (larger,
        # src contains "hcaptcha-challenge" or "imgs") is the one with the prompt.
        try:
            result = self._js(self._tab_id, """(function(){
                // Try main page overlay elements first
                var prompts = document.querySelectorAll(
                    '.challenge-prompt, .prompt-text, [class*="prompt"]'
                );
                for (var p of prompts) {
                    var t = (p.textContent || '').trim();
                    // Filter out checkbox-related text
                    if (t.length > 10 && t.length < 200
                        && !t.includes('checkbox') && !t.includes('Widget')) return t;
                }
                // Try challenge iframe title (NOT checkbox iframe)
                var iframes = document.querySelectorAll('iframe[src*="hcaptcha"]');
                for (var f of iframes) {
                    var src = f.getAttribute('src') || '';
                    if (src.includes('checkbox')) continue;
                    var r = f.getBoundingClientRect();
                    // Challenge iframe is large (>300px wide), checkbox is small
                    if (r.width < 300) continue;
                    var title = f.getAttribute('title') || '';
                    if (title.length > 5 && !title.includes('checkbox') && !title.includes('Widget'))
                        return title;
                }
                return '';
            })()""", timeout=5.0)

            text = str(result).strip().strip('"').strip("'")
            # Filter out generic/useless titles
            if (text and len(text) > 10
                    and "checkbox" not in text.lower()
                    and "widget" not in text.lower()
                    and text.lower() != "hcaptcha challenge"
                    and not text.lower().startswith("hcaptcha")):
                return text
        except Exception:
            pass

        # Method 2: Try CDP createIsolatedWorld to read inside the iframe
        ws_url = self._ensure_cdp_ws_url()
        if ws_url:
            try:
                text = self._cdp_read_challenge_text(ws_url)
                if text:
                    return text
            except Exception as exc:
                self._log(f"CDP challenge text extraction failed: {exc}")

        return ""

    def _cdp_read_challenge_text(self, ws_url: str) -> str:
        """Read challenge prompt text from inside hCaptcha iframe via CDP.

        hCaptcha challenge iframes are separate CDP targets (cross-process).
        We find the challenge iframe target via /json endpoint and connect
        directly to its WebSocket to read the DOM.
        """
        import websockets

        cdp_port = self._discover_cdp_port()
        if not cdp_port:
            return ""

        # Find challenge iframe target directly
        try:
            tabs = json.loads(
                urllib.request.urlopen(f"http://localhost:{cdp_port}/json", timeout=5).read()
            )
        except Exception:
            return ""

        challenge_target = next(
            (t for t in tabs
             if "hcaptcha" in t.get("url", "") and "challenge" in t.get("url", "")
             and t.get("webSocketDebuggerUrl")),
            None,
        )
        if not challenge_target:
            return ""

        challenge_ws = challenge_target["webSocketDebuggerUrl"]

        async def _read() -> str:
            async with websockets.connect(challenge_ws, max_size=10 * 1024 * 1024) as ws:
                await ws.send(json.dumps({
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": """
                            (function(){
                                var el = document.querySelector('.prompt-text, .task-image .prompt-text, h2');
                                if (el) return el.textContent.trim();
                                var all = document.querySelectorAll('span, div, h2, h3, p');
                                for (var e of all) {
                                    var t = e.textContent.trim();
                                    if ((t.includes('click') || t.includes('Select') || t.includes('containing')
                                         || t.includes('Find') || t.includes('Pick') || t.includes('Choose')
                                         || t.includes('every') || t.includes('each'))
                                        && t.length > 10 && t.length < 200) return t;
                                }
                                return '';
                            })()
                        """,
                        "returnByValue": True,
                    },
                }))
                resp = await asyncio.wait_for(ws.recv(), timeout=5)
                data = json.loads(resp)
                value = data.get("result", {}).get("result", {}).get("value", "")
                return str(value).strip() if value else ""

        try:
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, _read()).result(timeout=10)
        except Exception:
            return ""

    def _read_grid_positions(self) -> dict | None:
        """Read the hCaptcha iframe bounding rect and compute cell CSS positions.

        The grid layout percentages are calibrated against a 320x490 iframe
        (the standard hCaptcha challenge size) and validated with CDP
        Input.dispatchMouseEvent clicks.

        Returns dict with:
          - cells: {"row,col": (css_x, css_y), ...} — center of each cell
          - verify: (css_x, css_y) — center of verify/next button
          - iframe: {x, y, w, h} — the iframe bounding rect
        Or None if no visible challenge iframe exists.
        """
        try:
            result_str = self._js(self._tab_id, """(function(){
                var iframes = document.querySelectorAll('iframe[src*="hcaptcha"]');
                for (var f of iframes) {
                    var src = f.getAttribute('src') || '';
                    if (src.includes('checkbox')) continue;
                    var r = f.getBoundingClientRect();
                    if (r.width >= 300 && r.height >= 400 && r.y > -100 && r.y < 800) {
                        return JSON.stringify({x: r.x, y: r.y, w: r.width, h: r.height});
                    }
                }
                return JSON.stringify({error: 'no iframe'});
            })()""", timeout=10.0)

            if not result_str or "error" in str(result_str):
                self._log(f"grid read: {result_str}")
                return None

            # The MCP _js() may return the JSON as a string, or sometimes
            # double-encode it. Parse until we get a dict.
            iframe = result_str
            for _ in range(3):
                if isinstance(iframe, str):
                    try:
                        iframe = json.loads(iframe)
                    except (json.JSONDecodeError, ValueError):
                        break
                else:
                    break

            if not isinstance(iframe, dict) or "x" not in iframe:
                self._log(f"grid read: unexpected format: {str(result_str)[:100]}")
                return None

            info = self._compute_grid_from_iframe(iframe)
            self._log(
                f"grid: iframe at ({iframe['x']:.0f},{iframe['y']:.0f}) "
                f"{iframe['w']:.0f}x{iframe['h']:.0f}, "
                f"verify at {info['verify']}"
            )
            return info

        except Exception as exc:
            self._log(f"grid read error: {exc}")
            return None

    @staticmethod
    def _compute_grid_from_iframe(iframe: dict) -> dict:
        """Compute cell CSS coords from iframe bounding rect.

        Percentages proven on 320x490 iframe via CDP dispatchMouseEvent:
          - Grid top: 34% of iframe height from top
          - Grid left: 5% of iframe width from left
          - Cell width: 29.5% of iframe width
          - Cell height: 17.5% of iframe height
          - Cell gap X: 0.8% of iframe width
          - Cell gap Y: 0.5% of iframe height
          - Verify button: 82% X, 93% Y within iframe
        """
        ix, iy = iframe["x"], iframe["y"]
        iw, ih = iframe["w"], iframe["h"]

        grid_top = iy + ih * 0.34
        grid_left = ix + iw * 0.05
        cell_w = iw * 0.295
        cell_h = ih * 0.175
        gap_x = iw * 0.008
        gap_y = ih * 0.005

        cells: dict[str, tuple[int, int]] = {}
        for row in range(3):
            for col in range(3):
                cx = grid_left + col * (cell_w + gap_x) + cell_w / 2
                cy = grid_top + row * (cell_h + gap_y) + cell_h / 2
                cells[f"{row},{col}"] = (round(cx), round(cy))

        verify = (round(ix + iw * 0.82), round(iy + ih * 0.93))

        return {"cells": cells, "verify": verify, "iframe": iframe}

    # ── CDP WebSocket helpers (screenshots + clicks) ──────────────────

    def _ensure_cdp_ws_url(self) -> str | None:
        """Discover and cache the CDP WebSocket URL for the suno tab."""
        if self._cdp_ws_url:
            return self._cdp_ws_url

        try:
            cdp_port = self._discover_cdp_port()
            tabs = json.loads(
                urllib.request.urlopen(
                    f"http://localhost:{cdp_port}/json", timeout=5
                ).read()
            )
            suno_tab = next(
                (t for t in tabs if t.get("type") == "page" and "suno" in t.get("url", "")),
                None,
            )
            if suno_tab:
                self._cdp_ws_url = suno_tab["webSocketDebuggerUrl"]
                return self._cdp_ws_url
        except Exception as exc:
            self._log(f"CDP discovery error: {exc}")
        return None

    def _take_screenshot(self, crop_to_iframe: dict | None = None) -> bool:
        """Take a screenshot and save to disk.

        Tries CDP WebSocket first (higher resolution, DPR-aware), falls back
        to BrowserOS MCP screenshot (tab-targeted, more reliable tab routing).

        If crop_to_iframe is provided (dict with x, y, w, h in CSS pixels),
        the screenshot is cropped to just the captcha iframe area. This
        dramatically improves vision classification accuracy since the model
        sees only the captcha grid, not the full page.
        """
        # Method 1: CDP WebSocket screenshot
        ws_url = self._ensure_cdp_ws_url()
        if ws_url:
            try:
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    img_data = pool.submit(
                        self._cdp_screenshot_sync, ws_url
                    ).result(timeout=15)

                if img_data and len(img_data) > 1000:
                    if crop_to_iframe:
                        img_data = self._crop_to_iframe(img_data, crop_to_iframe)
                    # Sanity check: captcha-cropped image should be >50KB
                    # (a challenge grid with images is typically 200-500KB).
                    # If it's very small, it might have captured the wrong page.
                    if crop_to_iframe and len(img_data) < 50000:
                        self._log(f"CDP screenshot suspiciously small ({len(img_data)} bytes), trying BrowserOS fallback")
                    else:
                        Path(SCREENSHOT_PATH).write_bytes(img_data)
                        self._log(f"CDP screenshot saved ({len(img_data)} bytes)")
                        return True
            except Exception as exc:
                self._log(f"CDP screenshot error: {exc}")

        # Method 2: BrowserOS MCP screenshot (fallback)
        try:
            resp = self._mcp_call({
                "name": "browser_get_screenshot",
                "arguments": {"tabId": self._tab_id, "size": "large"},
            }, timeout=15.0)
            # BrowserOS returns base64 image in result content
            result = resp.get("result", {})
            content = result.get("content", []) if isinstance(result, dict) else []
            for item in (content if isinstance(content, list) else []):
                if isinstance(item, dict) and item.get("type") == "image":
                    img_b64 = item.get("data", "")
                    if img_b64:
                        img_data = base64.b64decode(img_b64)
                        if crop_to_iframe:
                            img_data = self._crop_to_iframe(img_data, crop_to_iframe)
                        Path(SCREENSHOT_PATH).write_bytes(img_data)
                        self._log(f"BrowserOS screenshot saved ({len(img_data)} bytes)")
                        return True
        except Exception as exc:
            self._log(f"BrowserOS screenshot error: {exc}")

        self._log("screenshot: all methods failed")
        return False

    @staticmethod
    def _crop_to_iframe(img_data: bytes, iframe: dict) -> bytes:
        """Crop a full-page CDP screenshot to just the iframe area.

        CDP Page.captureScreenshot returns at devicePixelRatio scale.
        The iframe dict has CSS pixel coords — we read the actual image
        dimensions to compute the scale factor.
        """
        try:
            from PIL import Image
            import io

            img = Image.open(io.BytesIO(img_data))
            img_w, img_h = img.size

            # Infer DPR from image dimensions vs a reasonable CSS viewport
            # (we know the viewport is ~540 CSS wide from BrowserOS)
            # Fallback: estimate DPR from image width / 540
            dpr = img_w / 540.0 if img_w > 600 else 1.0

            # Compute crop box in device pixels (with padding)
            pad = 5  # small padding in CSS pixels
            x1 = max(0, int((iframe["x"] - pad) * dpr))
            y1 = max(0, int((iframe["y"] - pad) * dpr))
            x2 = min(img_w, int((iframe["x"] + iframe["w"] + pad) * dpr))
            y2 = min(img_h, int((iframe["y"] + iframe["h"] + pad) * dpr))

            cropped = img.crop((x1, y1, x2, y2))

            buf = io.BytesIO()
            cropped.save(buf, format="PNG")
            return buf.getvalue()

        except Exception:
            # If cropping fails, return original screenshot
            return img_data

    def _click_at(self, x: int, y: int) -> None:
        """Click at CSS viewport coordinates via CDP Input.dispatchMouseEvent.

        BrowserOS browser_click_coordinates does NOT register on cross-origin
        hCaptcha iframes. CDP dispatchMouseEvent works because it operates at
        the browser compositor level, correctly routing events into OOPIFs.
        """
        ws_url = self._ensure_cdp_ws_url()
        if not ws_url:
            self._log(f"click: no CDP WebSocket URL for ({x},{y})")
            return

        try:
            with concurrent.futures.ThreadPoolExecutor() as pool:
                pool.submit(self._cdp_click_sync, ws_url, x, y).result(timeout=10)
        except Exception as exc:
            self._log(f"click error at ({x},{y}): {exc}")

    @staticmethod
    def _cdp_click_sync(ws_url: str, x: float, y: float) -> None:
        """Dispatch mousePressed + mouseReleased via CDP WebSocket (sync wrapper)."""
        asyncio.run(BrowserOSVisionSolver._cdp_click_async(ws_url, x, y))

    @staticmethod
    async def _cdp_click_async(ws_url: str, x: float, y: float) -> None:
        """Dispatch mousePressed + mouseReleased via CDP WebSocket."""
        import websockets

        async with websockets.connect(ws_url, max_size=10 * 1024 * 1024) as ws:
            await ws.send(json.dumps({
                "id": 1,
                "method": "Input.dispatchMouseEvent",
                "params": {
                    "type": "mousePressed",
                    "x": x, "y": y,
                    "button": "left", "clickCount": 1,
                },
            }))
            await asyncio.wait_for(ws.recv(), timeout=5)

            await ws.send(json.dumps({
                "id": 2,
                "method": "Input.dispatchMouseEvent",
                "params": {
                    "type": "mouseReleased",
                    "x": x, "y": y,
                    "button": "left", "clickCount": 1,
                },
            }))
            await asyncio.wait_for(ws.recv(), timeout=5)

    @staticmethod
    def _cdp_screenshot_sync(ws_url: str) -> bytes:
        """Take CDP screenshot from a sync/thread context."""
        return asyncio.run(BrowserOSVisionSolver._cdp_screenshot_async(ws_url))

    @staticmethod
    async def _cdp_screenshot_async(ws_url: str) -> bytes:
        """Take CDP screenshot via WebSocket."""
        import websockets

        async with websockets.connect(ws_url, max_size=50 * 1024 * 1024) as ws:
            await ws.send(json.dumps({
                "id": 1,
                "method": "Page.captureScreenshot",
                "params": {"format": "png"},
            }))
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            d = json.loads(raw)
            data_b64 = d.get("result", {}).get("data", "")
            if data_b64:
                return base64.b64decode(data_b64)
        return b""

    @staticmethod
    def _discover_cdp_port() -> int:
        """Auto-discover CDP port from BrowserOS server_config.json."""
        config_path = Path.home() / ".config/browser-os/.browseros/server_config.json"
        if config_path.exists():
            try:
                data = json.loads(config_path.read_text())
                return data.get("ports", {}).get("cdp", 9119)
            except Exception:
                pass
        return 9119

    def _poll_token_sync(self) -> str | None:
        """Poll for captcha token via JS."""
        result_str = self._js(self._tab_id, """(function(){
            var t = window.__bvs_token || '';
            try { var r = hcaptcha.getResponse() || ''; if (r.length > t.length) t = r; } catch(e){}
            return t;
        })()""")
        m = re.search(r"P1_[A-Za-z0-9_\-\.]+", str(result_str))
        if m and len(m.group(0)) > 100:
            return m.group(0)
        return None

    async def _wait_for_iframe(self, deadline: float) -> bool:
        """Wait for hCaptcha challenge iframe to appear."""
        while time.monotonic() < min(deadline, time.monotonic() + 15):
            if self._check_iframe_visible():
                return True
            await asyncio.sleep(1)
        return False

    def _check_iframe_visible(self) -> bool:
        """Check if an hCaptcha CHALLENGE iframe is visible (on-screen).

        Distinguishes the challenge grid iframe (tall, src contains 'challenge')
        from the checkbox iframe (short, src contains 'checkbox').
        """
        result = self._js(self._tab_id, """(function(){
            var iframes = document.querySelectorAll('iframe[src*="hcaptcha"]');
            for (var f of iframes) {
                var src = f.getAttribute('src') || '';
                if (src.includes('checkbox')) continue;
                var r = f.getBoundingClientRect();
                if (r.width >= 300 && r.height >= 400 && r.y > -100 && r.y < 800) return 'visible';
            }
            return 'none';
        })()""")
        return "visible" in str(result).lower()

    def _cleanup_container(self) -> None:
        """Remove the widget container we created."""
        try:
            self._js(self._tab_id, """(function(){
                var el = document.getElementById('hcap-bvs-solve');
                if (el) el.remove();
                try { delete window.__bvs_token; } catch(e){}
            })()""")
        except Exception:
            pass

    def _log(self, msg: str) -> None:
        if self._verbose:
            print(f"  [browseros_vision] {msg}")
        try:
            from .log import get_logger
            get_logger("captcha_solver").debug(msg, extra={"tag": "browseros_vision"})
        except Exception:
            pass
