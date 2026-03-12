"""Solve hCaptcha on suno.com via Chrome CDP + AI vision.

Connects to Chrome CDP (local or tunneled) and solves hCaptcha challenges
using Kimi K2.5 / Llama 3.2 vision models. Uses CDP Input events for
clicking (works over SSH tunnel, no xdotool needed).

Usage:
    # Direct (from gpu-dev-3 where Chrome runs)
    uv run python tools/solve_suno_captcha.py --cdp-port 9222

    # Via SSH tunnel (from main-dev-2)
    ssh -f -N -L 19222:localhost:9222 100.116.10.41
    uv run python tools/solve_suno_captcha.py --cdp-port 19222

    # Save token to gpu-dev-3 env
    uv run python tools/solve_suno_captcha.py --cdp-port 19222 --save-remote

Requires: captcha-kit[cdp] (pip install -e ../captcha-kit[cdp])
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import httpx
import websockets
from PIL import Image

from captcha_kit.util.image import build_composite_grid
from captcha_kit.util.parse import parse_cell_response

# ── Config ───────────────────────────────────────────────
SITEKEY = "d65453de-3f1a-4aac-9366-a0f06e52b2ce"
PAGE_URL = "https://suno.com/create"
GPU_HOST = "100.116.10.41"  # gpu-dev-3

# ── API Key loading ──────────────────────────────────────


def load_api_keys() -> tuple[str, str]:
    """Load NVIDIA and Kimi API keys from env or ~/.env.suno."""
    nvidia = os.environ.get("NVIDIA_KIMI_API_KEY", "").strip()
    kimi = os.environ.get("KIMI_API_KEY", "").strip()

    for p in (Path.home() / ".env.suno", Path.home() / ".env"):
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            if line.startswith("NVIDIA_KIMI_API_KEY=") and not nvidia:
                nvidia = line.split("=", 1)[1].strip().strip("'\"")
            elif line.startswith("KIMI_API_KEY=") and not kimi:
                kimi = line.split("=", 1)[1].strip().strip("'\"")

    # Also try loading from gpu-dev-3
    if not nvidia or not kimi:
        try:
            result = subprocess.run(
                ["ssh", GPU_HOST, "cat ~/.env.suno"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                if line.startswith("NVIDIA_KIMI_API_KEY=") and not nvidia:
                    nvidia = line.split("=", 1)[1].strip().strip("'\"")
                elif line.startswith("KIMI_API_KEY=") and not kimi:
                    kimi = line.split("=", 1)[1].strip().strip("'\"")
        except Exception:
            pass

    return nvidia, kimi


# ── CDP transport ──────────────────────────────────────

class CdpSession:
    """Chrome DevTools Protocol session."""

    def __init__(self, ws, *, verbose: bool = True):
        self._ws = ws
        self._msg_id = 0
        self._verbose = verbose
        self.dpr = 1.0

    def _nid(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def send(self, method: str, params: dict | None = None, timeout: float = 12) -> dict:
        mid = self._nid()
        await self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        dl = time.time() + timeout
        while time.time() < dl:
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=min(3, dl - time.time()))
                d = json.loads(raw)
                if d.get("id") == mid:
                    if "error" in d:
                        return {}
                    return d.get("result", {})
            except asyncio.TimeoutError:
                continue
            except Exception:
                return {}
        return {}

    async def js(self, expr: str, timeout: float = 10):
        r = await self.send("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True,
        }, timeout)
        return r.get("result", {}).get("value")

    async def screenshot(self, path: str) -> bytes | None:
        r = await self.send("Page.captureScreenshot", {"format": "png"})
        if "data" in r:
            data = base64.b64decode(r["data"])
            Path(path).write_bytes(data)
            return data
        return None

    async def click(self, x: float, y: float):
        """Click at CSS viewport coordinates via CDP Input events."""
        await self.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": int(x), "y": int(y), "button": "none",
        })
        await asyncio.sleep(random.uniform(0.05, 0.15))
        await self.send("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": int(x), "y": int(y),
            "button": "left", "clickCount": 1,
        })
        await asyncio.sleep(random.uniform(0.04, 0.12))
        await self.send("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": int(x), "y": int(y),
            "button": "left", "clickCount": 1,
        })
        await asyncio.sleep(random.uniform(0.1, 0.3))

    async def calibrate(self):
        d = await self.js("window.devicePixelRatio")
        if d:
            self.dpr = float(d)

    def log(self, msg: str):
        if self._verbose:
            print(f"  {msg}")


# ── Challenge interaction ──────────────────────────────

async def render_widget(cdp_s: CdpSession):
    """Clean up old widgets and render a fresh hCaptcha widget."""
    await cdp_s.js(
        "(function(){"
        "document.querySelectorAll('[data-hcaptcha-widget-id]').forEach(function(el){el.remove();});"
        "var old=document.getElementById('cdp-hcaptcha');if(old)old.remove();"
        "try{hcaptcha.reset();}catch(e){}"
        "try{delete window.__hcap_auto;}catch(e){}"
        "})()"
    )
    await asyncio.sleep(0.5)

    await cdp_s.js(
        "(function(){"
        "var c=document.createElement('div');"
        "c.id='cdp-hcaptcha';"
        "c.style.cssText='position:fixed;bottom:80px;left:50%25;transform:translateX(-50%25);z-index:99999;';"
        "document.body.appendChild(c);"
        "window.__hcap_auto='';"
        f"hcaptcha.render('cdp-hcaptcha',{{"
        f"sitekey:'{SITEKEY}',"
        "size:'normal',"
        "callback:function(t){window.__hcap_auto=t;}"
        "});"
        "})()"
    )
    await asyncio.sleep(2)


async def find_checkbox(cdp_s: CdpSession) -> dict | None:
    """Find the hCaptcha checkbox iframe position."""
    pos_str = await cdp_s.js(
        "(function(){"
        "var iframes=document.querySelectorAll('iframe');"
        "for(var f of iframes){"
        "var src=f.src||'';"
        "if(src.indexOf('hcaptcha')===-1)continue;"
        "var r=f.getBoundingClientRect();"
        "if(r.width>250&&r.width<350&&r.height<100&&r.y>0&&r.y<2000){"
        "return JSON.stringify({x:r.x,y:r.y,w:r.width,h:r.height});"
        "}}"
        "return null;"
        "})()"
    )
    return json.loads(pos_str) if pos_str else None


async def find_challenge(cdp_s: CdpSession) -> dict | None:
    """Find the hCaptcha challenge iframe (400x600)."""
    info = await cdp_s.js(
        "(function(){"
        "var iframes=document.querySelectorAll('iframe');"
        "for(var f of iframes){"
        "var src=f.src||'';"
        "if(src.indexOf('hcaptcha')===-1)continue;"
        "var r=f.getBoundingClientRect();"
        "if(r.width>=350&&r.height>=400&&r.y>0&&r.y<1200){"
        "return JSON.stringify({x:r.x,y:r.y,w:r.width,h:r.height});"
        "}}"
        "return null;"
        "})()"
    )
    return json.loads(info) if info else None


async def get_grid(cdp_s: CdpSession) -> dict | None:
    """Extract grid cells, buttons, and prompt from the captcha iframe."""
    frame_tree = await cdp_s.send("Page.getFrameTree")

    def find_hc(node):
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

    world = await cdp_s.send("Page.createIsolatedWorld", {
        "frameId": hc_frame["id"], "worldName": "grid_probe",
    })
    ctx = world.get("executionContextId")
    if not ctx:
        return None

    r = await cdp_s.send("Runtime.evaluate", {
        "expression": (
            "(function(){"
            "var all=document.querySelectorAll('*');"
            "var cells=[];"
            "for(var el of all){"
            "var style=getComputedStyle(el);"
            "var bg=style.backgroundImage||'';"
            "if(bg&&bg!=='none'&&bg.indexOf('url')>-1){"
            "var r=el.getBoundingClientRect();"
            "if(r.width>50&&r.height>50){"
            "cells.push({x:r.x,y:r.y,w:r.width,h:r.height});"
            "}}}"
            "cells.sort(function(a,b){return a.y!==b.y?a.y-b.y:a.x-b.x;});"
            "var btns=[];"
            "for(var b of all){"
            "var text=(b.textContent||'').trim().toLowerCase();"
            "if((text==='verify'||text==='next'||text==='skip')&&b.getBoundingClientRect().width>30){"
            "var r=b.getBoundingClientRect();"
            "btns.push({text:text,x:r.x+r.width/2,y:r.y+r.height/2,w:r.width,h:r.height});"
            "}}"
            "var prompt='';"
            "for(var el of all){"
            "var t=(el.textContent||'').trim();"
            "if((t.indexOf('lick')>-1||t.indexOf('ap on')>-1||t.indexOf('elect')>-1)&&t.length<120&&el.children.length<5){prompt=t;break;}"
            "}"
            "return JSON.stringify({cells:cells,buttons:btns,prompt:prompt});"
            "})()"
        ),
        "contextId": ctx,
        "returnByValue": True,
    })
    val = r.get("result", {}).get("value")
    return json.loads(val) if val else None


async def poll_token(cdp_s: CdpSession, timeout: float = 8) -> str | None:
    """Poll for hCaptcha token."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        token = await cdp_s.js(
            "(function(){"
            "try{var r=hcaptcha.getResponse()||'';if(r.length>100&&r.startsWith('P1_'))return r;}catch(e){}"
            "var a=window.__hcap_auto||'';"
            "if(a.length>100&&a.startsWith('P1_'))return a;"
            "return '';"
            "})()"
        )
        if token and isinstance(token, str) and len(token) > 100:
            return token
        await asyncio.sleep(1)
    return None


# ── Cell cropping and classification ────────────────────

def crop_cells(screenshot_bytes: bytes, iframe_pos: dict, grid_data: dict, dpr: float = 1.0):
    """Crop individual cells from screenshot."""
    img = Image.open(io.BytesIO(screenshot_bytes))
    ix, iy = iframe_pos["x"], iframe_pos["y"]
    cells = grid_data.get("cells", [])
    if len(cells) < 2:
        return [], None

    grid_cells = cells[1:10] if len(cells) > 9 else cells[1:]
    results = []
    for i, cell in enumerate(grid_cells):
        row, col = i // 3, i % 3
        sx = int((ix + cell["x"]) * dpr)
        sy = int((iy + cell["y"]) * dpr)
        sw = int(cell["w"] * dpr)
        sh = int(cell["h"] * dpr)
        cropped = img.crop((sx, sy, sx + sw, sy + sh))
        results.append((row, col, cropped))

    ref_img = None
    if cells:
        ref = cells[0]
        rx = int((ix + ref["x"]) * dpr)
        ry = int((iy + ref["y"]) * dpr)
        rw = int(ref["w"] * dpr)
        rh = int(ref["h"] * dpr)
        ref_img = img.crop((rx, ry, rx + rw, ry + rh))
        ref_img.save("/tmp/cdp_ref.png")

    return results, ref_img


async def classify_cells(
    cell_images: list[tuple[int, int, Image.Image]],
    prompt: str,
    *,
    nvidia_key: str = "",
    kimi_key: str = "",
) -> list[tuple[int, int]]:
    """Classify cells using AI vision API."""
    composite_b64 = build_composite_grid(cell_images, ref_path="/tmp/cdp_ref.png")

    # Clean up prompt (remove "Please select an image to report." noise)
    clean_prompt = prompt
    for noise in ("Please select an image to report.", "Please click on", "Please select"):
        clean_prompt = clean_prompt.replace(noise, "").strip()
    clean_prompt = clean_prompt.rstrip(".")

    text_prompt = (
        f'You are solving a visual captcha challenge.\n'
        f'The challenge says: "{clean_prompt}"\n\n'
        f'The image shows a REFERENCE image on the left and a 3x3 grid on the right.\n'
        f'Each cell is labeled (row,col) from (0,0) top-left to (2,2) bottom-right.\n\n'
        f'Look at each cell carefully. Which cells contain the object/thing described?\n'
        f'Do NOT select all cells - typically 2-4 cells match.\n'
        f'Return ONLY a JSON array of [row, col] pairs.\n'
        f'Example: [[0,1],[1,2]]\nReturn raw JSON only, no explanation.'
    )

    content = [
        {"type": "text", "text": text_prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{composite_b64}"}},
    ]

    # Try NVIDIA NIM — Llama 3.2 first (faster, more direct), then Kimi
    if nvidia_key:
        for model in ("meta/llama-3.2-90b-vision-instruct", "moonshotai/kimi-k2.5"):
            is_kimi = "kimi" in model.lower()
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(120.0 if is_kimi else 60.0)) as client:
                    r = await client.post(
                        "https://integrate.api.nvidia.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {nvidia_key}", "Content-Type": "application/json"},
                        json={
                            "model": model,
                            "messages": [{"role": "user", "content": content}],
                            "max_tokens": 4096 if is_kimi else 300,
                            "temperature": 0.1,
                        },
                    )
                    data = r.json()
                    if r.status_code != 200:
                        print(f"  [vision] {model}: {r.status_code} {str(data)[:150]}")
                        continue
                    msg = data.get("choices", [{}])[0].get("message", {})
                    text = msg.get("content") or ""
                    # For Kimi, also check reasoning_content
                    if is_kimi and not text:
                        text = msg.get("reasoning_content") or ""
                    print(f"  [vision] {model}: {text[:150]}")
                    result = parse_cell_response(text)
                    if result and len(result) < 8:
                        return result
                    elif result:
                        print(f"  [vision] {model}: rejected ({len(result)} cells)")
            except Exception as e:
                print(f"  [vision] {model}: {e}")

    # Fallback: Kimi direct API
    if kimi_key:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
                r = await client.post(
                    "https://api.kimi.com/coding/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {kimi_key}",
                        "Content-Type": "application/json",
                        "User-Agent": "claude-code/1.0",
                    },
                    json={
                        "model": "k2p5",
                        "messages": [{"role": "user", "content": content}],
                        "max_tokens": 4096,
                        "temperature": 0.1,
                    },
                )
                if r.status_code == 200:
                    msg = r.json().get("choices", [{}])[0].get("message", {})
                    text = msg.get("content") or ""
                    print(f"  [vision] kimi-direct: {text[:150]}")
                    result = parse_cell_response(text)
                    if result and len(result) < 8:
                        return result
        except Exception as e:
            print(f"  [vision] kimi-direct: {e}")

    return []


# ── Token saving ──────────────────────────────

def save_token_local(token: str):
    """Save token to local temp files."""
    Path("/tmp/suno_generate_token.txt").write_text(token)
    Path("/tmp/suno_captcha_token.txt").write_text(token)
    print(f"  [save] local: /tmp/suno_generate_token.txt ({len(token)} chars)")


def save_token_remote(token: str):
    """Save token to gpu-dev-3 ~/.env.suno and /tmp files."""
    try:
        # Save to /tmp files
        subprocess.run(
            ["ssh", GPU_HOST, f"echo '{token}' > /tmp/suno_generate_token.txt"],
            timeout=10, capture_output=True,
        )

        # Update ~/.env.suno
        update_cmd = (
            f"python3 -c \""
            f"import pathlib; "
            f"p=pathlib.Path.home()/'.env.suno'; "
            f"lines=p.read_text().splitlines() if p.exists() else []; "
            f"new=[l for l in lines if not l.strip().startswith('SUNO_GENERATE_TOKEN=')]; "
            f"new.append('SUNO_GENERATE_TOKEN={token}'); "
            f"p.write_text(chr(10).join(new)+chr(10)); "
            f"print('updated')\""
        )
        result = subprocess.run(
            ["ssh", GPU_HOST, update_cmd],
            capture_output=True, text=True, timeout=15,
        )
        print(f"  [save] remote: {result.stdout.strip()}")
    except Exception as e:
        print(f"  [save] remote error: {e}")


# ── Main solver ──────────────────────────────

async def solve(
    cdp_port: int = 19222,
    max_rounds: int = 6,
    save_remote: bool = False,
    verbose: bool = True,
) -> str | None:
    """Run the full hCaptcha solve flow. Returns token or None."""
    nvidia_key, kimi_key = load_api_keys()
    if not nvidia_key and not kimi_key:
        print("ERROR: No vision API keys found (NVIDIA_KIMI_API_KEY or KIMI_API_KEY)")
        return None

    # Find suno.com tab
    try:
        tabs_raw = urllib.request.urlopen(f"http://localhost:{cdp_port}/json", timeout=5).read()
        tabs = json.loads(tabs_raw)
    except Exception as e:
        print(f"ERROR: Cannot connect to CDP at port {cdp_port}: {e}")
        return None

    tab = next((t for t in tabs if t.get("type") == "page" and "suno.com" in t.get("url", "")), None)
    if not tab:
        print("ERROR: No suno.com tab found in Chrome")
        return None

    ws_url = tab["webSocketDebuggerUrl"]
    # If tunneled, fix the ws URL host
    if cdp_port != 9222:
        ws_url = re.sub(r"ws://[^/]+", f"ws://localhost:{cdp_port}", ws_url)

    print(f"[connect] {ws_url[:80]}...")

    ws = await websockets.connect(ws_url, max_size=50 * 1024 * 1024, ping_interval=30, ping_timeout=120)
    try:
        s = CdpSession(ws, verbose=verbose)
        for domain in ("Page", "Runtime", "Input", "DOM"):
            await s.send(f"{domain}.enable")

        await s.calibrate()
        print(f"[calibrate] DPR={s.dpr}")

        # Clear ALL hCaptcha-related cookies and storage
        print("[setup] clearing hCaptcha state...")
        await s.send("Network.enable")
        # Get ALL cookies
        all_cookies = await s.send("Network.getAllCookies")
        deleted = 0
        for cookie in all_cookies.get("cookies", []):
            name = cookie.get("name", "")
            domain = cookie.get("domain", "")
            if any(kw in domain.lower() for kw in ("hcaptcha", "hcap")):
                await s.send("Network.deleteCookies", {"name": name, "domain": domain})
                s.log(f"deleted: {name} ({domain})")
                deleted += 1
        print(f"[setup] cleared {deleted} hCaptcha cookies")
        # Also clear hcaptcha-related localStorage/sessionStorage
        await s.js("try{var keys=Object.keys(localStorage);keys.forEach(function(k){if(k.indexOf('hcap')>-1||k.indexOf('hcaptcha')>-1)localStorage.removeItem(k);})}catch(e){}")
        await s.js("try{var keys=Object.keys(sessionStorage);keys.forEach(function(k){if(k.indexOf('hcap')>-1||k.indexOf('hcaptcha')>-1)sessionStorage.removeItem(k);})}catch(e){}")

        # Always navigate fresh after clearing cookies to reset hCaptcha state
        print("[navigate] navigating fresh to reset hCaptcha state...")
        try:
            await s.send("Page.navigate", {"url": PAGE_URL})
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass
        print("[navigate] waiting 15s for page load...")
        await asyncio.sleep(15)

        # Reconnect websocket
        tabs_raw = urllib.request.urlopen(
            f"http://localhost:{cdp_port}/json", timeout=5
        ).read()
        tabs_list = json.loads(tabs_raw)
        t = next((t for t in tabs_list if t.get("type") == "page"
                   and "suno.com" in t.get("url", "")), None)
        if not t:
            print("ERROR: No suno.com tab after navigation")
            return None
        new_ws_url = t["webSocketDebuggerUrl"]
        if cdp_port != 9222:
            new_ws_url = re.sub(r"ws://[^/]+", f"ws://localhost:{cdp_port}", new_ws_url)
        print(f"[reconnect] {new_ws_url[:80]}...")
        ws = await websockets.connect(new_ws_url, max_size=50 * 1024 * 1024,
                                      ping_interval=30, ping_timeout=120)
        s = CdpSession(ws, verbose=verbose)
        for domain in ("Page", "Runtime", "Input", "DOM"):
            await s.send(f"{domain}.enable")

        # Wait for hcaptcha JS; inject if not loaded by page
        print("[wait] checking for hcaptcha JS...")
        hc_ready = await s.js("typeof hcaptcha")
        if hc_ready not in ("object", "function"):
            print("[inject] hcaptcha not found, injecting hcaptcha.js API...")
            await s.js(
                "(function(){"
                "var s=document.createElement('script');"
                "s.src='https://js.hcaptcha.com/1/api.js?render=explicit';"
                "s.async=true;"
                "document.head.appendChild(s);"
                "})()"
            )
            for attempt in range(15):
                hc_ready = await s.js("typeof hcaptcha")
                if hc_ready in ("object", "function"):
                    print(f"[inject] hcaptcha loaded after {attempt+1}s")
                    break
                await asyncio.sleep(2)
            else:
                print("ERROR: hcaptcha JS failed to load even after injection")
                await s.screenshot("/tmp/cdp_debug.png")
                return None
        else:
            print("[wait] hcaptcha already available")

        # Render widget and click checkbox
        await render_widget(s)

        checkbox = await find_checkbox(s)
        if checkbox:
            cx = int(checkbox["x"] + 30 + random.randint(0, 10))
            cy = int(checkbox["y"] + checkbox["h"] / 2 + random.randint(-3, 3))
            # Slow mouse movement toward checkbox (anti-bot evasion)
            for i in range(8):
                t = (i + 1) / 8
                ix = int(400 + (cx - 400) * t)
                iy = int(400 + (cy - 400) * t)
                await s.send("Input.dispatchMouseEvent", {
                    "type": "mouseMoved", "x": ix, "y": iy, "button": "none"
                })
                await asyncio.sleep(random.uniform(0.1, 0.2))
            print(f"[checkbox] clicking at CSS ({cx},{cy})")
            await s.click(cx, cy)
            await asyncio.sleep(5)
        else:
            print("[checkbox] not found, trying execute()")
            await s.js("hcaptcha.execute()")
            await asyncio.sleep(4)

        # Check auto-solve
        token = await poll_token(s, timeout=3)
        if token:
            print(f"\n>>> AUTO-SOLVED!")
            save_token_local(token)
            if save_remote:
                save_token_remote(token)
            return token

        # Wait for challenge
        iframe_pos = None
        for _ in range(30):
            iframe_pos = await find_challenge(s)
            if iframe_pos:
                break
            token = await poll_token(s, timeout=1)
            if token:
                print(f"\n>>> AUTO-SOLVED!")
                save_token_local(token)
                if save_remote:
                    save_token_remote(token)
                return token
            await asyncio.sleep(1)

        if not iframe_pos:
            print("ERROR: No challenge appeared")
            await s.screenshot("/tmp/cdp_debug.png")
            return None

        print(f"[challenge] iframe: {iframe_pos}")

        # Wait for grid images to load
        await asyncio.sleep(3)

        grid_data = None
        for retry in range(5):
            grid_data = await get_grid(s)
            if grid_data and len(grid_data.get("cells", [])) >= 2:
                break
            print(f"  [grid] retry {retry+1}: {len((grid_data or {}).get('cells', []))} cells")
            await asyncio.sleep(2)

        if not grid_data or not grid_data.get("cells"):
            print(f"ERROR: Could not extract grid: {grid_data}")
            await s.screenshot("/tmp/cdp_debug.png")
            return None

        prompt = grid_data.get("prompt", "")
        ncells = len(grid_data.get("cells", []))
        nbtns = len(grid_data.get("buttons", []))
        print(f"[challenge] \"{prompt}\" ({ncells} cells, {nbtns} buttons)")

        # Solve rounds
        for round_num in range(1, max_rounds + 1):
            print(f"\n--- Round {round_num} ---")

            ss = await s.screenshot(f"/tmp/cdp_r{round_num}.png")
            if not ss:
                break

            cell_images, ref_img = crop_cells(ss, iframe_pos, grid_data, s.dpr)
            if not cell_images:
                print("  no cells cropped")
                break

            print(f"  [crop] {len(cell_images)} cells, ref={'yes' if ref_img else 'no'}")

            cells_to_click = await classify_cells(
                cell_images, prompt, nvidia_key=nvidia_key, kimi_key=kimi_key
            )

            if not cells_to_click:
                print("  [skip] no cells classified, clicking verify/skip")
                ix, iy = iframe_pos["x"], iframe_pos["y"]
                btns = grid_data.get("buttons", [])
                btn = next((b for b in btns if b["text"] in ("skip", "next", "verify")), None)
                if btn:
                    await s.click(ix + btn["x"], iy + btn["y"])
                await asyncio.sleep(2)
                token = await poll_token(s, timeout=5)
                if token:
                    print(f"\n>>> SOLVED (skip)!")
                    save_token_local(token)
                    if save_remote:
                        save_token_remote(token)
                    return token
                continue

            print(f"  [click] {cells_to_click}")
            ix, iy = iframe_pos["x"], iframe_pos["y"]
            grid_cells = grid_data.get("cells", [])[1:10]
            order = list(cells_to_click)
            random.shuffle(order)
            for row, col in order:
                idx = row * 3 + col
                if idx >= len(grid_cells):
                    continue
                cell = grid_cells[idx]
                await s.click(ix + cell["x"] + cell["w"] / 2, iy + cell["y"] + cell["h"] / 2)
                await asyncio.sleep(random.uniform(0.3, 0.7))

            # Click verify
            await asyncio.sleep(0.5)
            btns = grid_data.get("buttons", [])
            fresh_grid = await get_grid(s)
            if fresh_grid:
                btns = fresh_grid.get("buttons", []) or btns
            btn = next((b for b in btns if b["text"] in ("verify", "next")), None)
            if not btn:
                btn = next((b for b in btns if b["text"] == "skip"), None)
            if btn:
                print(f"  [verify] clicking '{btn['text']}'")
                await s.click(ix + btn["x"], iy + btn["y"])
            else:
                print(f"  [verify] fallback click")
                await s.click(ix + 350, iy + 570)
            await asyncio.sleep(2)

            token = await poll_token(s, timeout=5)
            if token:
                print(f"\n>>> SOLVED in round {round_num}!")
                save_token_local(token)
                if save_remote:
                    save_token_remote(token)
                return token

            # Check for new challenge
            iframe_pos = await find_challenge(s)
            if not iframe_pos:
                token = await poll_token(s, timeout=3)
                if token:
                    print(f"\n>>> SOLVED!")
                    save_token_local(token)
                    if save_remote:
                        save_token_remote(token)
                    return token
                print("  iframe disappeared")
                break

            grid_data = await get_grid(s)
            if grid_data and grid_data.get("cells"):
                prompt = grid_data.get("prompt", prompt)
                print(f"  [new] \"{prompt}\"")
            else:
                print("  could not read new grid")
                break

        print("\nFAILED: exhausted all rounds")
        return None
    finally:
        try:
            await ws.close()
        except Exception:
            pass


def ensure_tunnel(cdp_port: int) -> int:
    """Ensure SSH tunnel exists to gpu-dev-3 for CDP access."""
    if cdp_port == 9222:
        return cdp_port  # Direct connection

    # Check if tunnel already exists
    try:
        urllib.request.urlopen(f"http://localhost:{cdp_port}/json/version", timeout=3)
        return cdp_port
    except Exception:
        pass

    # Create tunnel
    print(f"[tunnel] creating SSH tunnel localhost:{cdp_port} -> {GPU_HOST}:9222...")
    subprocess.run(
        ["ssh", "-f", "-N", "-L", f"{cdp_port}:localhost:9222", GPU_HOST],
        timeout=15,
    )
    time.sleep(2)

    try:
        urllib.request.urlopen(f"http://localhost:{cdp_port}/json/version", timeout=5)
        print(f"[tunnel] established")
        return cdp_port
    except Exception as e:
        print(f"ERROR: tunnel failed: {e}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Solve hCaptcha on suno.com via CDP + AI vision")
    parser.add_argument("--cdp-port", type=int, default=19222, help="CDP port (default: 19222 via tunnel)")
    parser.add_argument("--save-remote", action="store_true", help="Save token to gpu-dev-3 ~/.env.suno")
    parser.add_argument("--max-rounds", type=int, default=6, help="Max challenge rounds")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    print("=" * 60)
    print("Suno hCaptcha Solver (CDP + AI Vision)")
    print("=" * 60)

    port = ensure_tunnel(args.cdp_port)
    token = asyncio.run(solve(
        cdp_port=port,
        max_rounds=args.max_rounds,
        save_remote=args.save_remote,
        verbose=not args.quiet,
    ))

    if token:
        print(f"\nToken: {token[:60]}...")
        print(f"Length: {len(token)}")
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
