# MusicGen Deployment

This folder deploys a basic MusicGen HTTP API on a remote machine.

## API

- `GET /health`
- `POST /generate`

Example request:

```json
{
  "prompt": "cinematic synthwave with driving drums",
  "max_new_tokens": 512,
  "guidance_scale": 3.0
}
```

## Remote deploy (direct Python service, recommended)

This mode works even when Podman/GPU runtime is limited on WSL hosts.

```bash
# sync files
scp -r deployment/musicgen root@gpu-dev-3:/srv/music-gen

# install deps + run service in tmux
tailscale ssh root@gpu-dev-3 'bash /srv/music-gen/run_remote.sh'

# test
tailscale ssh root@gpu-dev-3 'curl -s http://127.0.0.1:8010/health'
tailscale ssh root@gpu-dev-3 "curl -s -X POST http://127.0.0.1:8010/generate -H 'content-type: application/json' -d '{\"prompt\":\"uplifting electronic melody\",\"max_new_tokens\":128}'"
```

Outputs are written to `OUT_DIR`.
By default, `run_remote.sh` writes output to Windows `D:` via `/host/d/Music/music-gen` (or `/mnt/d/Music/music-gen` when that mount style is available).

## Remote deploy (control-panel one-command flow)

```bash
cd tools/control-panel

# Confirm GPU visibility
cargo run -- gpu-check --node gpu

# Deploy + start service (writes wav files to D:\\Music\\music-gen, and reads Suno from D:\\Music\\suno)
cargo run -- musicgen-deploy --node gpu --out-dir /host/d/Music/music-gen --suno-dir /host/d/Music/suno --musicgen-dir /host/d/Music/music-gen

# Run generation test
cargo run -- musicgen-test --node gpu --prompt "epic cinematic trailer drums" --max-new-tokens 256
```

Web player UI:
- `GET /player` for browser player
- `GET /tracks` for JSON library

## Remote deploy (with control-panel + Podman)

From repo root:

```bash
cd tools/control-panel

# 1) Verify local tooling and config
cargo run -- doctor
cargo run -- nodes

# 2) Make sure remote directory exists
cargo run -- exec --node gpu -- mkdir -p /srv/music-gen

# 3) Sync this deployment bundle
cargo run -- sync --node gpu --local ../../deployment/musicgen --remote /srv/music-gen --delete

# 4) Start API on remote host
cargo run -- exec --node gpu -- podman compose -f /srv/music-gen/compose.gpu.yml --env-file /srv/music-gen/.env.example up -d --build

# 5) Test health + generation
cargo run -- exec --node gpu -- curl -s http://127.0.0.1:8010/health
cargo run -- exec --node gpu -- curl -s -X POST http://127.0.0.1:8010/generate -H 'content-type: application/json' -d '{"prompt":"epic orchestral trailer, huge percussion"}'
```

## GPU readiness check

On the remote node:

```bash
tailscale ssh root@gpu-dev-3 'nvidia-smi'
```

If this fails, MusicGen runs on CPU fallback (`DEVICE=cpu`).
