# coverlab

Deterministic open-source pipeline to convert an input audio file into piano/orchestral covers.

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

## CLI

```bash
coverctl run <input_audio> --style piano|orchestra [--output-dir data/out] [--job-id JOB] [--transcriber basicpitch|mock]
```

`mock` transcriber exists for deterministic tests and CI smoke runs.

## Docker

```bash
docker compose up --build
```

## Project Structure

```text
coverctl/   CLI
api/        FastAPI scaffolding
worker/     worker scaffolding
pipeline/   deterministic stage functions
tests/      smoke + unit tests
```
