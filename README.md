# coverlab

Unified music generation workspace for two flows:

- deterministic local audio-to-piano/orchestral covers
- Suno-backed prompt generation and upload-to-cover pipelines

## Outputs

Given `input.mp3` or `input.wav`, the pipeline writes:

- `transcription.mid`
- `cover_piano.mid` + `cover_piano.wav` (for piano style)
- `cover_orchestra.mid` + `cover_orchestra.wav` (for orchestra style)
- `manifest.json`
- optional `report.json`

## Quickstart

```bash
uv sync --all-extras --group dev
coverctl run data/in/sample.wav --style piano
coverctl run data/in/sample.wav --style orchestra
```

For Suno flows, bootstrap the infrastructure first:

```bash
# Start everything (Chrome CDP, BrowserOS MCP, JWT refresh, auth validation)
bash scripts/bootstrap_suno_infra.sh

# Or just load credentials into current shell
source suno.env.sh
```

**Suno API**: `https://studio-api.prod.suno.com` (NOT `studio-api.suno.ai` which returns 503)
**Model**: Always `chirp-crow` (Suno v5). Never use older models.

## CLI

```bash
coverctl run <input_audio> --style piano|orchestra [--output-dir data/out] [--job-id JOB] [--transcriber basicpitch|mock]
```

`mock` transcriber exists for deterministic tests and CI smoke runs.

Unified Suno commands:

```bash
coverctl suno generate "anime opening with bright guitars" --title "Sky Signal"
coverctl suno cover /path/to/source.mp3 --tags "anime, j-rock, cinematic"
coverctl suno cover-batch /path/to/downloaded-tracks --tags "orchestral, emotional" --recursive
coverctl suno anime-batch /path/to/downloaded-anime --preset anime-orchestral --recursive
```

`anime-batch` is the consolidation entrypoint for the downloaded anime library:
it walks a folder of local source tracks, uploads them to Suno, generates covers
with the selected preset, and writes a batch `manifest.json` plus per-track
artifacts under `data/anime-covers/` by default.

ACE Step covers (via SSH to gpu-dev-3):

```bash
coverctl ace-step cover /path/to/song.wav --tags "anime, j-rock" --lyrics "romaji lyrics"
coverctl ace-step batch /path/to/songs/ --tags "anime, rock"
```

Unified anime pipeline (ACE Step + Suno, all presets):

```bash
coverctl anime-pipeline /path/to/songs/ --output-dir data/anime-covers
coverctl anime-pipeline --from-list data/anime-sources/anime_songs.json --engine suno
```

## Docker

```bash
docker compose up --build
```

## Remote GPU Control Panel

Use the Rust control CLI to run commands and manage Podman services on remote nodes (for example your GPU host over Tailscale):

```bash
cd tools/control-panel
cargo run -- nodes
cargo run -- exec --node gpu -- nvidia-smi
cargo run -- podman-ps --node gpu
```

See `tools/control-panel/README.md` and `tools/control-panel/control-panel.example.toml` for setup.

For GPU deployment details, see `deployment/musicgen/README.md`.

## Project Structure

```text
coverctl/   CLI
api/        FastAPI scaffolding
worker/     worker scaffolding
pipeline/   deterministic stage functions
suno_wrapper/ vendored Suno client + auth/download helpers
tests/      smoke + unit tests
```
