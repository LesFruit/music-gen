"""Optional YOLO-based hCaptcha solver.

Requires ``ultralytics`` and ``Pillow`` (install via ``pip install suno-wrapper[yolo]``).

Flow:
    1. Trigger hCaptcha challenge in BrowserOS
    2. Screenshot the challenge grid
    3. Split into 3x3 cells
    4. YOLO classify each cell
    5. Click matching cells
    6. Submit and retrieve token
"""

from __future__ import annotations

import asyncio
import base64
import io
import re
import time
from typing import Callable

try:
    from ultralytics import YOLO
    from PIL import Image

    _HAS_YOLO = True
except ImportError:
    _HAS_YOLO = False

# Map hCaptcha challenge labels to COCO class names that YOLO can detect.
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
}


class YoloCaptchaSolver:
    """Solve hCaptcha image challenges using a local YOLO model."""

    def __init__(
        self,
        *,
        tab_id: int,
        mcp_caller: Callable[..., dict],
        js_executor: Callable[..., str],
        confidence: float = 0.4,
        model_path: str = "yolov8n.pt",
    ) -> None:
        if not _HAS_YOLO:
            raise ImportError(
                "ultralytics and Pillow are required for YOLO solver. "
                "Install with: pip install suno-wrapper[yolo]"
            )
        self._tab_id = tab_id
        self._mcp_call = mcp_caller
        self._js = js_executor
        self._confidence = confidence
        self._model = YOLO(model_path)

    async def solve(self, max_wait: float = 60.0) -> str | None:
        """Attempt to solve the hCaptcha challenge. Returns token or None."""
        deadline = time.monotonic() + max_wait

        # 1. Find the widget and trigger challenge
        widget_id = self._js(self._tab_id, """
            var el = document.querySelector('[data-hcaptcha-widget-id]');
            el ? el.getAttribute('data-hcaptcha-widget-id') : '';
        """).strip().strip('"')

        if not widget_id:
            return None

        # Reset and execute to trigger the visual challenge
        self._js(self._tab_id, f"try {{ hcaptcha.reset('{widget_id}'); }} catch(e) {{}}")
        await asyncio.sleep(1.0)

        # Click the checkbox to trigger visual challenge
        self._js(self._tab_id, """
            var iframe = document.querySelector('iframe[src*="hcaptcha.com/checkbox"]');
            if (iframe) { iframe.click(); }
        """)
        await asyncio.sleep(2.0)

        # 2. Get the challenge label
        challenge_label = self._js(self._tab_id, """
            var frame = document.querySelector('iframe[src*="hcaptcha.com/challenge"]');
            if (!frame) { '' }
            else {
                var title = frame.contentDocument
                    ? frame.contentDocument.querySelector('.prompt-text')
                    : null;
                title ? title.textContent.trim().toLowerCase() : '';
            }
        """).strip().strip('"').lower()

        if not challenge_label:
            return None

        # Find matching COCO classes
        target_classes: list[str] = []
        for label_key, coco_names in LABEL_MAP.items():
            if label_key in challenge_label:
                target_classes.extend(coco_names)

        if not target_classes:
            return None

        # 3. Screenshot the challenge grid
        screenshot_b64 = self._js(self._tab_id, """
            var canvas = document.createElement('canvas');
            var frame = document.querySelector('iframe[src*="hcaptcha.com/challenge"]');
            // Return empty if we can't access the challenge
            frame ? 'HAS_FRAME' : '';
        """)

        # Use MCP screenshot tool instead
        try:
            resp = self._mcp_call(
                {"name": "browser_screenshot", "arguments": {"tabId": self._tab_id}},
                timeout=10.0,
            )
            result = resp.get("result", {})
            content = result.get("content", []) if isinstance(result, dict) else []
            img_data = ""
            for item in content:
                if isinstance(item, dict) and item.get("type") == "image":
                    img_data = item.get("data", "")
                    break
            if not img_data:
                return None
        except Exception:
            return None

        # 4. Decode and split into 3x3 grid
        try:
            img_bytes = base64.b64decode(img_data)
            img = Image.open(io.BytesIO(img_bytes))
        except Exception:
            return None

        # Run YOLO on the full screenshot
        results = self._model.predict(img, conf=self._confidence, verbose=False)
        if not results or len(results) == 0:
            return None

        # Find detected objects matching target classes
        detections = results[0]
        matching_cells: set[int] = set()

        # Estimate grid position (hCaptcha grid is typically in center)
        w, h = img.size
        grid_x = w * 0.25
        grid_y = h * 0.25
        grid_w = w * 0.5
        grid_h = h * 0.5
        cell_w = grid_w / 3
        cell_h = grid_h / 3

        for box in detections.boxes:
            cls_id = int(box.cls[0])
            cls_name = detections.names[cls_id]
            if cls_name in target_classes:
                # Get center of detection
                cx = float((box.xyxy[0][0] + box.xyxy[0][2]) / 2)
                cy = float((box.xyxy[0][1] + box.xyxy[0][3]) / 2)

                # Map to grid cell (0-8)
                col = int((cx - grid_x) / cell_w)
                row = int((cy - grid_y) / cell_h)
                col = max(0, min(2, col))
                row = max(0, min(2, row))
                cell_idx = row * 3 + col
                matching_cells.add(cell_idx)

        if not matching_cells:
            return None

        # 5. Click matching cells
        for cell_idx in sorted(matching_cells):
            row, col = divmod(cell_idx, 3)
            # Click center of the cell
            click_x = int(grid_x + col * cell_w + cell_w / 2)
            click_y = int(grid_y + row * cell_h + cell_h / 2)
            self._js(self._tab_id, f"""
                document.elementFromPoint({click_x}, {click_y})?.click();
            """)
            await asyncio.sleep(0.3)

        # 6. Submit
        await asyncio.sleep(0.5)
        self._js(self._tab_id, """
            var btn = document.querySelector('.button-submit');
            if (btn) btn.click();
        """)

        # 7. Wait for token
        end = min(time.monotonic() + 10.0, deadline)
        while time.monotonic() < end:
            await asyncio.sleep(1.0)
            token_str = self._js(self._tab_id, """
                try { hcaptcha.getResponse() || '' } catch(e) { '' }
            """)
            m = re.search(r"P1_[A-Za-z0-9_\-\.]+", token_str)
            if m and len(m.group(0)) > 100:
                return m.group(0)

        return None
