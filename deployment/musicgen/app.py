from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from transformers import AutoProcessor, MusicgenForConditionalGeneration

MODEL_ID = os.getenv("MODEL_ID", "facebook/musicgen-small")
DEFAULT_DEVICE = os.getenv("DEVICE", "cuda")
DEFAULT_MAX_NEW_TOKENS = int(os.getenv("MAX_NEW_TOKENS", "512"))
OUT_DIR = Path(os.getenv("OUT_DIR", "/host/d/Music/music-gen"))
SUNO_DIR = Path(os.getenv("SUNO_DIR", "/host/d/Music/suno"))
MUSICGEN_DIR = Path(os.getenv("MUSICGEN_DIR", str(OUT_DIR)))
OUT_DIR.mkdir(parents=True, exist_ok=True)
SUNO_DIR.mkdir(parents=True, exist_ok=True)
MUSICGEN_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="music-gen-api", version="0.1.0")
SUPPORTED_AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=3, max_length=400)
    max_new_tokens: int = Field(default=DEFAULT_MAX_NEW_TOKENS, ge=64, le=2048)
    guidance_scale: float = Field(default=3.0, ge=1.0, le=10.0)


class GenerateResponse(BaseModel):
    file: str
    sample_rate: int
    duration_seconds: float
    model: str
    device: str


class ModelRuntime:
    def __init__(self, model_id: str, device: str) -> None:
        self.model_id = model_id
        self.device = self._resolve_device(device)
        self.processor = AutoProcessor.from_pretrained(model_id)
        self.model = MusicgenForConditionalGeneration.from_pretrained(model_id)
        self.model.to(self.device)

    @staticmethod
    def _resolve_device(requested: str) -> str:
        if requested.startswith("cuda") and not torch.cuda.is_available():
            return "cpu"
        return requested


RUNTIME: ModelRuntime | None = None


def get_runtime() -> ModelRuntime:
    global RUNTIME
    if RUNTIME is None:
        RUNTIME = ModelRuntime(MODEL_ID, DEFAULT_DEVICE)
    return RUNTIME


def _list_tracks(base: Path, source: str) -> list[dict[str, Any]]:
    tracks: list[dict[str, Any]] = []
    if not base.exists():
        return tracks
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in SUPPORTED_AUDIO_EXTS:
            continue
        rel = p.relative_to(base)
        tracks.append(
            {
                "source": source,
                "name": p.name,
                "relative_path": rel.as_posix(),
                "url": f"/audio/{source}/{rel.as_posix()}",
                "size_bytes": p.stat().st_size,
            }
        )
    return tracks


def _safe_audio_path(source: str, relative_path: str) -> Path:
    source_map = {"suno": SUNO_DIR, "music-gen": MUSICGEN_DIR}
    base = source_map.get(source)
    if base is None:
        raise HTTPException(status_code=404, detail="unknown source")
    requested = (base / relative_path).resolve()
    base_resolved = base.resolve()
    if not str(requested).startswith(str(base_resolved)):
        raise HTTPException(status_code=400, detail="invalid path")
    if not requested.exists() or not requested.is_file():
        raise HTTPException(status_code=404, detail="track not found")
    return requested


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "model": MODEL_ID,
        "device": DEFAULT_DEVICE,
        "runtime_loaded": RUNTIME is not None,
        "cuda_available": torch.cuda.is_available(),
        "out_dir": str(OUT_DIR),
        "suno_dir": str(SUNO_DIR),
        "musicgen_dir": str(MUSICGEN_DIR),
    }


@app.get("/tracks")
def tracks() -> dict[str, Any]:
    suno_tracks = _list_tracks(SUNO_DIR, "suno")
    musicgen_tracks = _list_tracks(MUSICGEN_DIR, "music-gen")
    combined = suno_tracks + musicgen_tracks
    combined.sort(key=lambda t: t["name"].lower())
    return {"count": len(combined), "tracks": combined}


@app.get("/audio/{source}/{relative_path:path}")
def audio(source: str, relative_path: str) -> FileResponse:
    path = _safe_audio_path(source, relative_path)
    return FileResponse(path, media_type="audio/mpeg", filename=path.name)


@app.get("/player", response_class=HTMLResponse)
def player() -> str:
    return """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Music Library</title>
  <style>
    body { font-family: sans-serif; margin: 16px; background: #0f1115; color: #e6e6e6; }
    .row { display: flex; gap: 8px; margin-bottom: 12px; }
    input { flex: 1; padding: 8px; background: #171a21; color: #e6e6e6; border: 1px solid #2a3040; }
    button { padding: 8px 12px; background: #2e5cff; border: none; color: white; cursor: pointer; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid #2a3040; padding: 8px; text-align: left; }
    tr:hover { background: #171a21; }
    .pill { padding: 2px 6px; border-radius: 12px; font-size: 12px; }
    .suno { background: #6f3cff; }
    .music-gen { background: #00a36c; }
    audio { width: 100%; margin: 12px 0; }
  </style>
</head>
<body>
  <h2>Music Library: Suno + MusicGen</h2>
  <div class="row">
    <input id="q" placeholder="Filter by name..." />
    <button onclick="loadTracks()">Refresh</button>
  </div>
  <audio id="player" controls></audio>
  <table>
    <thead><tr><th>Source</th><th>Name</th><th>Path</th><th>Play</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <script>
    let allTracks = [];
    async function loadTracks() {
      const res = await fetch('/tracks');
      const data = await res.json();
      allTracks = data.tracks || [];
      render();
    }
    function render() {
      const q = document.getElementById('q').value.toLowerCase();
      const rows = document.getElementById('rows');
      rows.innerHTML = '';
      for (const t of allTracks) {
        if (q && !t.name.toLowerCase().includes(q) && !t.relative_path.toLowerCase().includes(q)) continue;
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td><span class="pill ${t.source}">${t.source}</span></td>
          <td>${t.name}</td>
          <td>${t.relative_path}</td>
          <td><button data-url="${t.url}">Play</button></td>`;
        tr.querySelector('button').onclick = (e) => {
          const player = document.getElementById('player');
          player.src = e.target.dataset.url;
          player.play();
        };
        rows.appendChild(tr);
      }
    }
    document.getElementById('q').addEventListener('input', render);
    loadTracks();
  </script>
</body>
</html>"""


@app.post("/generate", response_model=GenerateResponse)
def generate(payload: GenerateRequest) -> GenerateResponse:
    runtime = get_runtime()

    try:
        inputs = runtime.processor(text=[payload.prompt], padding=True, return_tensors="pt")
        inputs = {k: v.to(runtime.device) for k, v in inputs.items()}

        with torch.inference_mode():
            audio_values = runtime.model.generate(
                **inputs,
                do_sample=True,
                guidance_scale=payload.guidance_scale,
                max_new_tokens=payload.max_new_tokens,
            )

        sample_rate = runtime.model.config.audio_encoder.sampling_rate
        waveform = audio_values[0, 0].detach().cpu().float().numpy().astype(np.float32)
        duration_seconds = float(len(waveform) / sample_rate)

        ts = int(time.time())
        filename = f"musicgen_{ts}.wav"
        out_path = OUT_DIR / filename
        sf.write(out_path, waveform, sample_rate)

        return GenerateResponse(
            file=str(out_path),
            sample_rate=sample_rate,
            duration_seconds=duration_seconds,
            model=runtime.model_id,
            device=runtime.device,
        )
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail=f"generation failed: {exc}") from exc
