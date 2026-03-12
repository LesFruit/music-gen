"""CDP-based hCaptcha visual solver using xdotool for human-like interaction.

Connects directly to Chromium via Chrome DevTools Protocol (CDP),
reads the hCaptcha iframe DOM, crops cells from DPR-aware screenshots,
classifies cells via AI vision or YOLO, and clicks via xdotool.

Requires: websockets, Pillow, xdotool (system), Xvfb running on DISPLAY.
Optional: ultralytics (for YOLO cell classification).

Classification chain (first success wins):
1. AI Vision via NVIDIA NIM API — handles ALL challenge types including semantic
   ones like "items transported using reference object" (uses Llama 3.2 90B Vision;
   env: NVIDIA_MINIMAX_API_KEY or MINIMAX_API_KEY for direct fallback)
2. YOLO local — fast, free, but only handles COCO object classes

Based on the proven v9 solver flow:
1. Connect to CDP websocket (port from BrowserOS config)
2. Navigate to suno.com, open create panel, fill textareas, click Create
3. Wait for hCaptcha iframe
4. Read grid geometry from iframe DOM via Page.createIsolatedWorld
5. Crop cells from CDP screenshot (coordinates * DPR)
6. Classify cells via AI vision or YOLO
7. Click cells via xdotool with bezier mouse curves
8. Re-read verify button from iframe DOM, click it
9. Poll for hcaptcha token
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import random
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

try:
    import websockets
    _HAS_WS = True
except ImportError:
    _HAS_WS = False

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

try:
    from ultralytics import YOLO
    _HAS_YOLO = True
except ImportError:
    _HAS_YOLO = False


# COCO class names that map to common hCaptcha challenge labels
LABEL_MAP: dict[str, list[str]] = {
    "bus": ["bus"],
    "bicycle": ["bicycle"],
    "motorcycle": ["motorcycle"],
    "boat": ["boat"],
    "airplane": ["airplane", "aeroplane"],
    "truck": ["truck"],
    "traffic light": ["traffic light"],
    "fire hydrant": ["fire hydrant"],
    "stop sign": ["stop sign"],
    "car": ["car"],
    "train": ["train"],
    "horse": ["horse"],
    "cat": ["cat"],
    "dog": ["dog"],
    "bird": ["bird"],
}


def discover_cdp_port() -> int:
    """Auto-discover CDP port from BrowserOS server_config.json."""
    config_path = Path.home() / ".config/browser-os/.browseros/server_config.json"
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text())
            return data.get("ports", {}).get("cdp", 9119)
        except Exception:
            pass
    return 9119


def _env_fallback(key: str) -> str:
    """Read a key from ~/.env.suno or ~/.env."""
    for p in (Path.home() / ".env.suno", Path.home() / ".env"):
        if not p.exists():
            continue
        for line in p.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip().strip("'\"")
    return ""


# Vision API — NVIDIA NIM (free tier, supports multimodal vision models)
NVIDIA_NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_VISION_MODEL = "moonshotai/kimi-k2.5"
FALLBACK_VISION_MODEL = "meta/llama-3.2-90b-vision-instruct"

# MiniMax direct API (text-only, used for challenge semantic reasoning)
MINIMAX_BASE_URL = "https://api.minimax.io/v1"
MINIMAX_TEXT_MODEL = "MiniMax-M2.5"


class CdpCaptchaSolver:
    """Solve hCaptcha visually via CDP + xdotool + AI vision."""

    def __init__(
        self,
        *,
        cdp_port: int = 0,
        display: str = ":99",
        verbose: bool = True,
        yolo_model_path: str = "yolov8n.pt",
        vision_model: str = "",
        vision_api_key: str = "",
    ) -> None:
        self._cdp_port = cdp_port or discover_cdp_port()
        self._display = display
        self._verbose = verbose
        self._yolo_path = yolo_model_path
        self._yolo_model: Any = None

        # Vision API config — NVIDIA NIM for vision, MiniMax direct for text
        self._vision_model = (
            vision_model
            or os.environ.get("CAPTCHA_VISION_MODEL", "").strip()
            or DEFAULT_VISION_MODEL
        )
        # NVIDIA NIM API key (for vision models — Kimi key first, MiniMax key fallback)
        self._nvidia_key = (
            vision_api_key
            or os.environ.get("NVIDIA_KIMI_API_KEY", "").strip()
            or _env_fallback("NVIDIA_KIMI_API_KEY")
            or os.environ.get("NVIDIA_MINIMAX_API_KEY", "").strip()
            or _env_fallback("NVIDIA_MINIMAX_API_KEY")
        )
        # MiniMax direct API key (for text reasoning fallback)
        self._minimax_key = (
            os.environ.get("MINIMAX_API_KEY", "").strip()
            or _env_fallback("MINIMAX_API_KEY")
        )

        # Calibrated at connect time
        self._dpr = 1.5
        self._base_x = 10.5
        self._base_y = 173.5

        # xdotool env
        self._xenv = os.environ.copy()
        self._xenv["DISPLAY"] = display

        self._msg_id = 0

    # ── Public API ────────────────────────────────────────────────────

    async def solve(self, *, timeout: float = 180.0, max_rounds: int = 6) -> str | None:
        """Full solve flow: navigate → trigger captcha → solve → return token.

        Returns the P1_ token string on success, None on failure.
        """
        if not _HAS_WS:
            self._log("websockets not installed")
            return None
        if not _HAS_PIL:
            self._log("Pillow not installed")
            return None

        tab = self._find_cdp_tab()
        if not tab:
            self._log("no suno tab found via CDP")
            return None

        ws_url = tab["webSocketDebuggerUrl"]
        self._log(f"connecting to {ws_url[:50]}...")

        async with websockets.connect(ws_url, max_size=50 * 1024 * 1024) as ws:
            for domain in ("Page", "Runtime", "Input", "DOM"):
                await self._cdp(ws, f"{domain}.enable")

            await self._calibrate(ws)

            # Navigate and trigger captcha
            await self._navigate_and_trigger(ws)

            # Wait for captcha iframe
            iframe_pos = await self._wait_for_captcha(ws, timeout=30)
            if not iframe_pos:
                # Maybe generation started without captcha
                token = await self._poll_token(ws, timeout=3)
                if token:
                    self._log(f"token granted without captcha ({len(token)} chars)")
                    return token
                self._log("no captcha appeared")
                return None

            self._log(
                f"captcha at ({iframe_pos['x']:.0f},{iframe_pos['y']:.0f}) "
                f"{iframe_pos['w']}x{iframe_pos['h']}"
            )

            # Read grid from iframe DOM
            grid_data = await self._get_grid(ws)
            if not grid_data:
                self._log("could not read grid from iframe DOM")
                return None

            prompt = grid_data.get("prompt", "")
            self._log(f"challenge: {prompt}")
            self._log(f"cells: {len(grid_data.get('cells', []))}")

            # Solve loop
            deadline = time.time() + timeout
            for round_num in range(1, max_rounds + 1):
                if time.time() >= deadline:
                    break

                self._log(f"--- round {round_num} ---")

                # Take screenshot and crop cells
                ss = await self._screenshot(ws, f"/tmp/cdp_solve_r{round_num}.png")
                if not ss:
                    self._log("screenshot failed")
                    break

                cell_images = self._crop_cells(ss, iframe_pos, grid_data)
                if not cell_images:
                    self._log("no cells cropped")
                    break

                # Classify cells (vision API → YOLO fallback)
                cells_to_click = await self._classify_cells(cell_images, prompt)
                if not cells_to_click:
                    self._log("no classifier could identify matching cells")
                    return None  # fall through to next solver

                self._log(f"clicking {len(cells_to_click)} cells: {cells_to_click}")
                await self._click_cells(ws, cells_to_click, iframe_pos, grid_data)
                await asyncio.sleep(0.5)

                # Click verify
                self._log("clicking verify...")
                await self._click_verify(ws, iframe_pos, grid_data)
                await asyncio.sleep(2)

                # Check for token
                token = await self._poll_token(ws, timeout=5)
                if token:
                    self._log(f"solved! token: {len(token)} chars")
                    return token

                # Check if captcha still visible for next round
                iframe2 = await self._get_iframe_pos(ws)
                if not iframe2:
                    token = await self._poll_token(ws, timeout=3)
                    if token:
                        self._log(f"delayed token: {len(token)} chars")
                        return token
                    self._log("captcha dismissed without token")
                    break

                # Re-read grid for next round
                grid2 = await self._get_grid(ws)
                if grid2 and grid2.get("cells"):
                    grid_data = grid2
                    iframe_pos = iframe2
                    prompt = grid2.get("prompt", prompt)
                else:
                    self._log("could not re-read grid for next round")
                    break

            return None

    # ── CDP transport ────────────────────────────────────────────────

    def _nid(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def _cdp(self, ws: Any, method: str, params: dict | None = None, timeout: float = 12) -> dict:
        mid = self._nid()
        await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        dl = time.time() + timeout
        while time.time() < dl:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(2, dl - time.time()))
                d = json.loads(raw)
                if d.get("id") == mid:
                    return d.get("result", {}) if "error" not in d else {}
            except asyncio.TimeoutError:
                continue
            except Exception:
                return {}
        return {}

    async def _js(self, ws: Any, expr: str, timeout: float = 8) -> Any:
        r = await self._cdp(
            ws, "Runtime.evaluate",
            {"expression": expr, "returnByValue": True, "awaitPromise": True},
            timeout,
        )
        return r.get("result", {}).get("value")

    async def _screenshot(self, ws: Any, path: str) -> bytes | None:
        r = await self._cdp(ws, "Page.captureScreenshot", {"format": "png"})
        if "data" in r:
            data = base64.b64decode(r["data"])
            Path(path).write_bytes(data)
            return data
        return None

    def _find_cdp_tab(self) -> dict | None:
        try:
            tabs = json.loads(
                urllib.request.urlopen(
                    f"http://localhost:{self._cdp_port}/json", timeout=5
                ).read()
            )
            return next(
                (t for t in tabs if t.get("type") == "page" and "suno" in t.get("url", "")),
                None,
            )
        except Exception:
            return None

    # ── Calibration ──────────────────────────────────────────────────

    async def _calibrate(self, ws: Any) -> None:
        dpr = await self._js(ws, "window.devicePixelRatio")
        if dpr:
            self._dpr = float(dpr)
        self._base_x, self._base_y = 10.5, 173.5
        self._log(f"DPR={self._dpr} BASE=({self._base_x},{self._base_y})")

    # ── Mouse helpers (xdotool + bezier) ─────────────────────────────

    def _to_x11(self, css_x: float, css_y: float) -> tuple[int, int]:
        return (
            int(self._base_x + css_x * self._dpr),
            int(self._base_y + css_y * self._dpr),
        )

    @staticmethod
    def _bezier(p0: tuple, p1: tuple, steps: int = 12) -> list[tuple[int, int]]:
        ctrl = (
            (p0[0] + p1[0]) / 2 + random.uniform(-60, 60),
            (p0[1] + p1[1]) / 2 + random.uniform(-40, 40),
        )
        pts = []
        for i in range(steps + 1):
            t = i / steps
            x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * ctrl[0] + t ** 2 * p1[0]
            y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * ctrl[1] + t ** 2 * p1[1]
            pts.append((int(x), int(y)))
        return pts

    async def _human_move(self, x: int, y: int) -> None:
        try:
            r = subprocess.run(
                ["xdotool", "getmouselocation"],
                env=self._xenv, capture_output=True, text=True, timeout=3,
            )
            parts = r.stdout.split()
            cx = int(parts[0].split(":")[1])
            cy = int(parts[1].split(":")[1])
        except Exception:
            cx, cy = 500, 400

        pts = self._bezier((cx, cy), (x, y), steps=random.randint(8, 14))
        for p in pts[1:-1]:
            subprocess.run(
                ["xdotool", "mousemove", str(p[0]), str(p[1])],
                env=self._xenv, timeout=3,
            )
            await asyncio.sleep(random.uniform(0.008, 0.02))
        subprocess.run(
            ["xdotool", "mousemove", "--sync", str(x), str(y)],
            env=self._xenv, timeout=5,
        )
        await asyncio.sleep(random.uniform(0.05, 0.15))

    async def _human_click(self, x: int, y: int) -> None:
        jx = x + random.randint(-2, 2)
        jy = y + random.randint(-2, 2)
        await self._human_move(jx, jy)
        subprocess.run(["xdotool", "mousedown", "1"], env=self._xenv, timeout=3)
        await asyncio.sleep(random.uniform(0.04, 0.12))
        subprocess.run(["xdotool", "mouseup", "1"], env=self._xenv, timeout=3)
        await asyncio.sleep(random.uniform(0.1, 0.3))

    # ── Page interaction ─────────────────────────────────────────────

    async def _navigate_and_trigger(self, ws: Any) -> None:
        """Navigate to suno.com and programmatically trigger hCaptcha.

        Instead of finding the create form (which Suno keeps moving), we
        render an hCaptcha widget directly and execute it.  This works on
        any authenticated Suno page.
        """
        # Check if already on a Suno page
        url = await self._js(ws, "location.href")
        if not url or "suno.com" not in str(url):
            self._log("navigating to suno.com/create...")
            await self._cdp(ws, "Page.navigate", {"url": "https://suno.com/create"})
            await asyncio.sleep(8)
        else:
            self._log(f"already on {str(url)[:50]}")

        # Wait for page to load (hcaptcha JS must be available)
        for attempt in range(20):
            ready = await self._js(ws, "typeof hcaptcha !== 'undefined'")
            if ready:
                break
            if attempt == 10:
                # Reload if hcaptcha still not loaded
                await self._cdp(ws, "Page.navigate", {"url": "https://suno.com/create"})
            await asyncio.sleep(2)

        if not ready:
            self._log("hcaptcha JS not available on page")
            await self._screenshot(ws, "/tmp/cdp_no_hcaptcha.png")
            return

        self._log("hcaptcha JS loaded, rendering widget...")

        # Render hCaptcha widget and execute it to trigger the challenge
        widget_id = await self._js(ws, """(function(){
            // Remove any previous widget
            var old = document.getElementById('cdp-hcaptcha');
            if (old) old.remove();

            var container = document.createElement('div');
            container.id = 'cdp-hcaptcha';
            container.style.cssText = 'position:fixed;bottom:80px;left:50%;transform:translateX(-50%);z-index:99999;';
            document.body.appendChild(container);

            var wid = hcaptcha.render('cdp-hcaptcha', {
                sitekey: 'd65453de-3f1a-4aac-9366-a0f06e52b2ce',
                size: 'normal',
                callback: function(token) {
                    window.__hcap_auto = token;
                },
            });
            return wid;
        })()""")

        if not widget_id:
            self._log("failed to render hCaptcha widget")
            return

        self._log(f"hCaptcha widget rendered (id={widget_id}), executing...")
        await asyncio.sleep(1)

        # Execute to trigger the challenge popup
        await self._js(ws, "hcaptcha.execute()")
        await asyncio.sleep(3)

    # ── Captcha iframe detection ─────────────────────────────────────

    async def _get_iframe_pos(self, ws: Any) -> dict | None:
        """Get captcha iframe position in CSS coordinates."""
        info = await self._js(ws, """(function(){
            var iframes = document.querySelectorAll('iframe');
            for (var f of iframes) {
                var src = f.src || '';
                if (src.indexOf('hcaptcha') === -1) continue;
                var r = f.getBoundingClientRect();
                if (r.width >= 300 && r.height >= 400 && r.y > -100 && r.y < 800) {
                    return JSON.stringify({x: r.x, y: r.y, w: r.width, h: r.height});
                }
            }
            return null;
        })()""")
        return json.loads(info) if info else None

    async def _wait_for_captcha(self, ws: Any, timeout: float = 30) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            pos = await self._get_iframe_pos(ws)
            if pos:
                return pos
            # Check if token granted without captcha
            token = await self._poll_token(ws, timeout=1)
            if token:
                return None  # caller checks for this
            await asyncio.sleep(1)
        return None

    # ── Grid extraction from iframe DOM ──────────────────────────────

    async def _get_grid(self, ws: Any) -> dict | None:
        """Read cell positions, buttons, and prompt from hCaptcha iframe DOM."""
        frame_tree = await self._cdp(ws, "Page.getFrameTree")

        def find_hc(node: dict) -> dict | None:
            for child in node.get("childFrames", []):
                url = child.get("frame", {}).get("url", "")
                if "hcaptcha" in url:
                    return child["frame"]
                found = find_hc(child)
                if found:
                    return found
            return None

        hc_frame = find_hc(frame_tree.get("frameTree", {}))
        if not hc_frame:
            return None

        world = await self._cdp(ws, "Page.createIsolatedWorld", {
            "frameId": hc_frame["id"],
            "worldName": "grid_probe",
        })
        ctx = world.get("executionContextId")
        if not ctx:
            return None

        r = await self._cdp(ws, "Runtime.evaluate", {
            "expression": """(function(){
                var all = document.querySelectorAll('*');
                var cells = [];
                for (var el of all) {
                    var style = getComputedStyle(el);
                    var bg = style.backgroundImage || '';
                    if (bg && bg !== 'none' && bg.indexOf('url') > -1) {
                        var r = el.getBoundingClientRect();
                        if (r.width > 50 && r.height > 50) {
                            cells.push({
                                x: r.x, y: r.y, w: r.width, h: r.height,
                                cls: (el.className||'').substring(0, 30)
                            });
                        }
                    }
                }
                cells.sort(function(a,b){ return a.y !== b.y ? a.y - b.y : a.x - b.x; });

                var btns = [];
                var buttons = document.querySelectorAll('*');
                for (var b of buttons) {
                    var text = (b.textContent||'').trim().toLowerCase();
                    if ((text === 'verify' || text === 'next' || text === 'skip') && b.getBoundingClientRect().width > 30) {
                        var r = b.getBoundingClientRect();
                        btns.push({text: text, x: r.x + r.width/2, y: r.y + r.height/2, w: r.width, h: r.height});
                    }
                }

                var prompt = '';
                for (var el of all) {
                    var t = (el.textContent||'').trim();
                    if (t.indexOf('Click') > -1 && t.length < 120 && el.children.length < 5) {
                        prompt = t;
                        break;
                    }
                }

                return JSON.stringify({cells: cells, buttons: btns, prompt: prompt});
            })()""",
            "contextId": ctx,
            "returnByValue": True,
        })
        val = r.get("result", {}).get("value")
        return json.loads(val) if val else None

    # ── Cell cropping ────────────────────────────────────────────────

    def _crop_cells(
        self, screenshot_bytes: bytes, iframe_pos: dict, grid_data: dict,
    ) -> list[tuple[int, int, Image.Image]]:
        """Crop cells from CDP screenshot with DPR scaling.

        Returns list of (row, col, PIL.Image) tuples.
        """
        img = Image.open(io.BytesIO(screenshot_bytes))
        ix, iy = iframe_pos["x"], iframe_pos["y"]
        cells = grid_data.get("cells", [])

        if len(cells) < 2:
            return []

        # First cell is reference image, next 9 are grid cells
        grid_cells = cells[1:10] if len(cells) > 9 else cells[1:]
        dpr = self._dpr

        results = []
        for i, cell in enumerate(grid_cells):
            row, col = i // 3, i % 3
            page_x = ix + cell["x"]
            page_y = iy + cell["y"]
            sx = int(page_x * dpr)
            sy = int(page_y * dpr)
            sw = int(cell["w"] * dpr)
            sh = int(cell["h"] * dpr)

            cropped = img.crop((sx, sy, sx + sw, sy + sh))
            # Save for debugging
            cropped.save(f"/tmp/cdp_cell_{row}_{col}.png")
            results.append((row, col, cropped))

        # Crop and save reference image
        if cells:
            ref = cells[0]
            rx = int((ix + ref["x"]) * dpr)
            ry = int((iy + ref["y"]) * dpr)
            rw = int(ref["w"] * dpr)
            rh = int(ref["h"] * dpr)
            ref_crop = img.crop((rx, ry, rx + rw, ry + rh))
            ref_crop.save("/tmp/cdp_ref.png")

        return results

    # ── Cell classification ──────────────────────────────────────────

    async def _classify_cells(
        self, cell_images: list[tuple[int, int, Image.Image]], prompt: str,
        ref_image_path: str = "/tmp/cdp_ref.png",
    ) -> list[tuple[int, int]]:
        """Classify cells. Tries AI vision first, then YOLO fallback."""
        # 1. Try NVIDIA NIM vision (handles ALL challenge types)
        if self._nvidia_key:
            result = await self._classify_vision(cell_images, prompt, ref_image_path)
            if result:
                self._log(f"vision classified {len(result)} cells")
                return result
            self._log("vision classification returned no matches, trying YOLO")

        # 2. YOLO fallback (COCO classes only)
        result = self._classify_yolo(cell_images, prompt)
        if result:
            return result

        self._log("no classifier could handle this challenge")
        return []

    async def _classify_vision(
        self, cell_images: list[tuple[int, int, Image.Image]], prompt: str,
        ref_image_path: str,
    ) -> list[tuple[int, int]]:
        """Classify cells using a vision model via NVIDIA NIM API.

        Chain: primary vision model → fallback vision model → MiniMax text + YOLO combo.
        """
        import httpx

        # Build content payload (shared across vision model attempts)
        content = self._build_vision_content(cell_images, prompt, ref_image_path)

        # Try primary vision model, then fallback
        for model in (self._vision_model, FALLBACK_VISION_MODEL):
            self._log(f"asking {model} to classify {len(cell_images)} cells...")
            result = await self._call_vision_api(content, model)
            if result:
                return result

        # Last resort: use MiniMax M2.5 for text-based reasoning about the challenge
        if self._minimax_key:
            result = await self._classify_minimax_text(cell_images, prompt)
            if result:
                return result

        return []

    def _build_vision_content(
        self, cell_images: list[tuple[int, int, Image.Image]], prompt: str,
        ref_image_path: str,
    ) -> list[dict]:
        """Build a single composite grid image for vision API calls.

        Instead of sending 10+ separate image_url entries (which Llama rejects
        "at most 1 image" and Kimi times out on), we composite everything into
        ONE annotated PIL image:
        - Reference image (200x200) on the left with "REFERENCE" label
        - 3x3 cell grid (130x130 each) on the right, each labeled (row,col)
        - Single composite ~620x430px saved to /tmp/cdp_composite.png
        """
        from PIL import ImageDraw, ImageFont

        # Target sizes
        ref_size = (200, 200)
        cell_size = (130, 130)
        padding = 10
        label_h = 16  # height reserved for text labels

        # Layout: ref on left, 3x3 grid on right
        grid_w = 3 * cell_size[0] + 2 * padding
        grid_h = 3 * (cell_size[1] + label_h) + 2 * padding
        total_w = ref_size[0] + padding + grid_w + 2 * padding
        total_h = max(ref_size[1] + label_h + padding, grid_h) + 2 * padding

        composite = Image.new("RGB", (total_w, total_h), (255, 255, 255))
        draw = ImageDraw.Draw(composite)

        # Try to get a small font; fall back to default
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
        except Exception:
            font = ImageFont.load_default()

        # Draw reference image
        ref_path = Path(ref_image_path)
        if ref_path.exists():
            ref_img = Image.open(ref_path).resize(ref_size, Image.LANCZOS)
            ref_x, ref_y = padding, padding + label_h
            draw.text((padding, padding), "REFERENCE", fill=(200, 0, 0), font=font)
            composite.paste(ref_img, (ref_x, ref_y))

        # Draw 3x3 cell grid
        grid_origin_x = ref_size[0] + 2 * padding
        grid_origin_y = padding

        for row, col, img in cell_images:
            cx = grid_origin_x + col * (cell_size[0] + padding)
            cy = grid_origin_y + row * (cell_size[1] + label_h + padding)
            label = f"({row},{col})"
            draw.text((cx, cy), label, fill=(0, 0, 180), font=font)
            cell_resized = img.resize(cell_size, Image.LANCZOS)
            composite.paste(cell_resized, (cx, cy + label_h))

        # Save for debugging
        composite_path = "/tmp/cdp_composite.png"
        composite.save(composite_path, format="PNG")
        self._log(f"composite grid saved: {total_w}x{total_h}px → {composite_path}")

        # Build payload: 1 text + 1 image (works with ALL models)
        composite_b64 = base64.b64encode(Path(composite_path).read_bytes()).decode()
        content: list[dict] = [
            {
                "type": "text",
                "text": (
                    f"You are solving a visual captcha challenge.\n"
                    f"The challenge says: \"{prompt}\"\n\n"
                    f"The image shows a REFERENCE image on the left (labeled REFERENCE) "
                    f"and a 3x3 grid of cells on the right, each labeled (row,col) from "
                    f"(0,0) top-left to (2,2) bottom-right.\n\n"
                    f"Determine which cells match the challenge based on the reference image.\n"
                    f"Return ONLY a JSON array of [row, col] pairs, e.g. [[0,1],[1,2],[2,0]]\n"
                    f"If no cells match, return []\n"
                    f"IMPORTANT: Return raw JSON only, no markdown, no explanation."
                ),
            },
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{composite_b64}"},
            },
        ]
        return content

    async def _call_vision_api(
        self, content: list[dict], model: str,
    ) -> list[tuple[int, int]]:
        """Call NVIDIA NIM vision API with the given content and model."""
        import httpx

        # Kimi uses reasoning tokens and needs more max_tokens
        is_kimi = "kimi" in model.lower()
        max_tokens = 800 if is_kimi else 300

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0 if is_kimi else 60.0)) as client:
                r = await client.post(
                    f"{NVIDIA_NIM_BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._nvidia_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": content}],
                        "max_tokens": max_tokens,
                        "temperature": 0.1,
                    },
                )
                data = r.json()

                if r.status_code != 200:
                    err = data.get("error", {}).get("message", str(data))
                    self._log(f"vision API error ({r.status_code}, {model}): {err}")
                    return []

                msg = data.get("choices", [{}])[0].get("message", {})
                # Kimi K2.5 puts the final answer in "content" and reasoning in "reasoning_content"
                # Some models return content directly
                text = msg.get("content") or ""
                if not text.strip() and msg.get("reasoning_content"):
                    # Fallback: extract answer from reasoning if content is empty
                    text = msg["reasoning_content"]
                self._log(f"vision response ({model}): {text.strip()[:120]}")
                return self._parse_cell_response(text)

        except Exception as e:
            self._log(f"vision API call failed ({model}): {e}")
            return []

    async def _classify_minimax_text(
        self, cell_images: list[tuple[int, int, Image.Image]], prompt: str,
    ) -> list[tuple[int, int]]:
        """Use MiniMax M2.5 (text-only) to reason about challenge semantics + YOLO for images.

        MiniMax analyzes the challenge text to determine what object classes to look for,
        then YOLO classifies the actual images.
        """
        import httpx

        self._log(f"asking MiniMax M2.5 to analyze challenge: {prompt[:60]}...")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
                r = await client.post(
                    f"{MINIMAX_BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._minimax_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": MINIMAX_TEXT_MODEL,
                        "messages": [{"role": "user", "content": (
                            f"A visual captcha challenge says: \"{prompt}\"\n\n"
                            f"What object(s) should I look for in the grid images? "
                            f"List the objects as a JSON array of lowercase strings, "
                            f"e.g. [\"bus\", \"bicycle\"]. Return ONLY the JSON array."
                        )}],
                        "max_tokens": 100,
                        "temperature": 0.1,
                    },
                )
                data = r.json()
                if r.status_code != 200:
                    return []
                text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                # Strip thinking tags from MiniMax response
                import re
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
                self._log(f"MiniMax objects: {text[:80]}")

                # Parse object list and use YOLO to classify
                try:
                    objects = json.loads(text)
                    if isinstance(objects, list) and objects:
                        return self._classify_yolo_custom(cell_images, objects)
                except (json.JSONDecodeError, ValueError):
                    pass
        except Exception as e:
            self._log(f"MiniMax text call failed: {e}")
        return []

    def _classify_yolo_custom(
        self, cell_images: list[tuple[int, int, Image.Image]], target_labels: list[str],
    ) -> list[tuple[int, int]]:
        """Classify cells using YOLO with custom target labels from MiniMax reasoning."""
        if not _HAS_YOLO:
            return []
        if self._yolo_model is None:
            try:
                self._yolo_model = YOLO(self._yolo_path)
            except Exception as e:
                self._log(f"YOLO load failed: {e}")
                return []

        target_lower = [t.lower() for t in target_labels]
        clicks = []
        for row, col, img in cell_images:
            try:
                results = self._yolo_model.predict(img, conf=0.3, verbose=False)
                if results and len(results) > 0:
                    for box in results[0].boxes:
                        cls_id = int(box.cls[0])
                        cls_name = results[0].names[cls_id].lower()
                        if cls_name in target_lower or any(t in cls_name for t in target_lower):
                            clicks.append((row, col))
                            break
            except Exception:
                continue
        if clicks:
            self._log(f"YOLO (MiniMax-guided) classified {len(clicks)} cells")
        return clicks

    @staticmethod
    def _parse_cell_response(text: str) -> list[tuple[int, int]]:
        """Parse a JSON array of [row, col] from model response."""
        import re
        # Extract JSON array from potentially messy response
        # Try raw parse first
        text = text.strip()
        if text.startswith("```"):
            # Strip markdown code fences
            text = re.sub(r"^```\w*\n?", "", text)
            text = re.sub(r"\n?```$", "", text)
            text = text.strip()

        try:
            arr = json.loads(text)
            if isinstance(arr, list):
                return [(int(cell[0]), int(cell[1])) for cell in arr if len(cell) >= 2]
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

        # Fallback: find array pattern in text
        m = re.search(r"\[[\s\[\]\d,]+\]", text)
        if m:
            try:
                arr = json.loads(m.group(0))
                if isinstance(arr, list):
                    return [(int(cell[0]), int(cell[1])) for cell in arr if isinstance(cell, list) and len(cell) >= 2]
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

        return []

    def _classify_yolo(
        self, cell_images: list[tuple[int, int, Image.Image]], prompt: str,
    ) -> list[tuple[int, int]]:
        """Classify cells using local YOLO model (COCO classes only)."""
        if not _HAS_YOLO:
            return []

        prompt_lower = prompt.lower()
        target_classes: list[str] = []
        for label_key, coco_names in LABEL_MAP.items():
            if label_key in prompt_lower:
                target_classes.extend(coco_names)

        if not target_classes:
            return []

        if self._yolo_model is None:
            try:
                self._yolo_model = YOLO(self._yolo_path)
            except Exception as e:
                self._log(f"YOLO load failed: {e}")
                return []

        clicks = []
        for row, col, img in cell_images:
            try:
                results = self._yolo_model.predict(img, conf=0.35, verbose=False)
                if results and len(results) > 0:
                    for box in results[0].boxes:
                        cls_id = int(box.cls[0])
                        cls_name = results[0].names[cls_id]
                        if cls_name in target_classes:
                            clicks.append((row, col))
                            break
            except Exception:
                continue

        if clicks:
            self._log(f"YOLO classified {len(clicks)} cells")
        return clicks

    # ── Cell clicking ────────────────────────────────────────────────

    async def _click_cells(
        self, ws: Any, cells_to_click: list[tuple[int, int]],
        iframe_pos: dict, grid_data: dict,
    ) -> None:
        ix, iy = iframe_pos["x"], iframe_pos["y"]
        grid_cells = grid_data.get("cells", [])[1:10]

        order = list(cells_to_click)
        random.shuffle(order)

        for row, col in order:
            idx = row * 3 + col
            if idx >= len(grid_cells):
                continue

            cell = grid_cells[idx]
            page_x = ix + cell["x"] + cell["w"] / 2
            page_y = iy + cell["y"] + cell["h"] / 2
            x11_x, x11_y = self._to_x11(page_x, page_y)
            self._log(f"click ({row},{col}): X11({x11_x},{x11_y})")
            await self._human_click(x11_x, x11_y)
            await asyncio.sleep(random.uniform(0.3, 0.7))

    async def _click_verify(self, ws: Any, iframe_pos: dict, grid_data: dict) -> None:
        """Click verify/next button. Re-reads from iframe DOM after cell selection."""
        ix, iy = iframe_pos["x"], iframe_pos["y"]

        # Re-read buttons (they change from Skip to Verify after selecting cells)
        fresh_grid = await self._get_grid(ws)
        btns = (fresh_grid or {}).get("buttons", []) or grid_data.get("buttons", [])

        # Prefer verify/next over skip
        target = None
        for b in btns:
            if b["text"] in ("verify", "next"):
                target = b
                break
        if not target:
            for b in btns:
                if b["text"] == "skip":
                    target = b
                    break

        if target:
            page_x = ix + target["x"]
            page_y = iy + target["y"]
            x11_x, x11_y = self._to_x11(page_x, page_y)
            self._log(f"verify '{target['text']}': X11({x11_x},{x11_y})")
            await self._human_click(x11_x, x11_y)
        else:
            # Fallback: typical verify button position
            page_x = ix + 350
            page_y = iy + 570
            x11_x, x11_y = self._to_x11(page_x, page_y)
            self._log("verify fallback position")
            await self._human_click(x11_x, x11_y)

    # ── Token polling ────────────────────────────────────────────────

    async def _poll_token(self, ws: Any, timeout: float = 8) -> str | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            token = await self._js(ws, """(function(){
                try {
                    var r = hcaptcha.getResponse() || '';
                    if (r.length > 100 && r.startsWith('P1_')) return r;
                } catch(e) {}
                var auto = window.__hcap_auto || '';
                if (auto.length > 100 && auto.startsWith('P1_')) return auto;
                return '';
            })()""")
            if token and isinstance(token, str) and len(token) > 100:
                return token
            await asyncio.sleep(1)
        return None

    # ── Logging ──────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        if self._verbose:
            print(f"  [cdp_solver] {msg}")
