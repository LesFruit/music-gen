# control-panel

Rust CLI for controlling remote machines (for example your GPU node) over Tailscale + SSH.

## What it does

- Lists configured nodes
- Runs arbitrary commands on a node over SSH
- Syncs local deployment files to remote nodes (`sync`)
- Runs common Podman service actions (`status`, `start`, `stop`, `restart`, `logs`)
- Checks Podman containers on a node (`podman ps`)
- Checks GPU readiness on a node (`gpu-check`)
- Deploys and starts MusicGen (`musicgen-deploy`)
- Runs a MusicGen generation test (`musicgen-test`)

## Config

Create a config file at either:
- `~/.config/music-gen/control-panel.toml` (default), or
- local `control-panel.toml` in this directory.

You can also override with `MUSIC_GEN_CONTROL_PANEL_CONFIG=/path/to/file.toml`.

Example config:

```toml
[node.gpu]
ssh_user = "ubuntu"
host = "100.88.12.34"
workdir = "/srv/music-gen"

[service.music_gen_api]
node = "gpu"
container = "music-gen-api"

[service.music_gen_worker]
node = "gpu"
container = "music-gen-worker"
```

## Usage

```bash
cd tools/control-panel
cargo run -- doctor
cargo run -- nodes
cargo run -- gpu-check --node gpu
cargo run -- sync --node gpu --local ../../deployment/musicgen --remote /srv/music-gen --delete
cargo run -- musicgen-deploy --node gpu --out-dir /host/d/Music/music-gen --suno-dir /host/d/Music/suno --musicgen-dir /host/d/Music/music-gen
cargo run -- musicgen-test --node gpu --prompt "epic cinematic trailer drums" --max-new-tokens 256
cargo run -- podman-ps --node gpu
cargo run -- exec --node gpu -- nvidia-smi
cargo run -- service --name music_gen_api --action status
cargo run -- service --name music_gen_worker --action restart
cargo run -- service --name music_gen_worker --action logs --tail 200
```

## Notes

- Requires `ssh` available on the machine where this CLI runs.
- Assumes key-based SSH auth is already set up for your remote nodes.
- If Tailscale SSH prompts for browser approval, complete it once and rerun the command.
