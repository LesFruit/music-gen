#!/usr/bin/env bash
set -euo pipefail

cd /srv/music-gen

if ! command -v python3 >/dev/null 2>&1; then
  apt-get update
  apt-get install -y python3 python3-venv python3-pip curl
fi

if ! command -v tmux >/dev/null 2>&1; then
  apt-get update
  apt-get install -y tmux
fi

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

export MODEL_ID="${MODEL_ID:-facebook/musicgen-small}"
export MAX_NEW_TOKENS="${MAX_NEW_TOKENS:-256}"
if [ -z "${OUT_DIR:-}" ]; then
  if [ -d /host/d ]; then
    export OUT_DIR="/host/d/Music/music-gen"
  elif [ -d /mnt/d ]; then
    export OUT_DIR="/mnt/d/Music/music-gen"
  else
    export OUT_DIR="/srv/music-gen/out"
  fi
fi
mkdir -p "$OUT_DIR"

if [ -z "${SUNO_DIR:-}" ]; then
  if [ -d /host/d ]; then
    export SUNO_DIR="/host/d/Music/suno"
  elif [ -d /mnt/d ]; then
    export SUNO_DIR="/mnt/d/Music/suno"
  else
    export SUNO_DIR="/srv/music-gen/suno"
  fi
fi
mkdir -p "$SUNO_DIR"

if [ -z "${MUSICGEN_DIR:-}" ]; then
  export MUSICGEN_DIR="$OUT_DIR"
fi
mkdir -p "$MUSICGEN_DIR"

# WSL NVIDIA bridge libs are required for CUDA/NVML discovery.
export LD_LIBRARY_PATH="/usr/lib/wsl/lib:/usr/lib/wsl/drivers${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# Auto-select CUDA when available unless DEVICE is explicitly provided.
if [ -z "${DEVICE:-}" ]; then
  if python - <<'PY' >/dev/null 2>&1
import torch
raise SystemExit(0 if torch.cuda.is_available() else 1)
PY
  then
    export DEVICE="cuda"
  else
    export DEVICE="cpu"
  fi
else
  export DEVICE
fi

# Restart background service cleanly in detached tmux session
tmux kill-session -t musicgen 2>/dev/null || true
tmux new-session -d -s musicgen ". /srv/music-gen/.venv/bin/activate && cd /srv/music-gen && LD_LIBRARY_PATH='${LD_LIBRARY_PATH}' MODEL_ID='${MODEL_ID}' DEVICE='${DEVICE}' MAX_NEW_TOKENS='${MAX_NEW_TOKENS}' OUT_DIR='${OUT_DIR}' SUNO_DIR='${SUNO_DIR}' MUSICGEN_DIR='${MUSICGEN_DIR}' uvicorn app:app --host 0.0.0.0 --port 8010 >/srv/music-gen/server.log 2>&1"
sleep 5

curl -sf http://127.0.0.1:8010/health || (echo 'health check failed'; tail -n 200 /srv/music-gen/server.log; exit 1)

echo "music-gen service started"
