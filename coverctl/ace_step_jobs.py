"""ACE Step cover generation via SSH to gpu-dev-3."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any

# ── Configuration ─────────────────────────────────────────────────────

ACE_STEP_HOST = "100.116.10.41"
ACE_STEP_DIR = "/srv/ace-step"
ACE_STEP_REMOTE_STAGING = "/tmp/ace-step-input"
ACE_STEP_LD_LIBRARY_PATH = "/usr/lib/wsl/lib:/usr/lib/wsl/drivers"
ACE_STEP_VENV_PYTHON = f"{ACE_STEP_DIR}/.venv/bin/python"

SUPPORTED_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".mp4"}

DEFAULT_NOISE_STRENGTH = 0.25
DEFAULT_DURATION = 60
DEFAULT_TAGS = "anime, j-rock, cover"
DEFAULT_OUTPUT_DIR = "/host/d/Music/ace-step/output/"


def add_ace_step_subcommands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    ace = subparsers.add_parser("ace-step", help="ACE Step cover generation via gpu-dev-3")
    ace_sub = ace.add_subparsers(dest="ace_step_command", required=True)

    cover = ace_sub.add_parser("cover", help="Generate a single ACE Step cover")
    cover.add_argument("input", type=Path, help="Audio file to cover")
    cover.add_argument("--tags", default=DEFAULT_TAGS)
    cover.add_argument("--lyrics", default="")
    cover.add_argument("--lyrics-file", type=Path, default=None, help="Path to lyrics text file")
    cover.add_argument(
        "--noise-strength",
        type=float,
        default=DEFAULT_NOISE_STRENGTH,
        help="Cover noise strength (0=faithful, 1=creative)",
    )
    cover.add_argument("--duration", type=int, default=DEFAULT_DURATION)
    cover.add_argument("--title", default="")
    cover.add_argument("--output-dir", type=Path, default=Path("data/ace-step-covers"))
    cover.add_argument("--remote-output-dir", default=DEFAULT_OUTPUT_DIR)
    cover.set_defaults(func=run_ace_step_cover)

    batch = ace_sub.add_parser("batch", help="Batch ACE Step covers for all files in a folder")
    batch.add_argument("input_dir", type=Path)
    batch.add_argument("--tags", default=DEFAULT_TAGS)
    batch.add_argument("--lyrics", default="")
    batch.add_argument(
        "--noise-strength",
        type=float,
        default=DEFAULT_NOISE_STRENGTH,
    )
    batch.add_argument("--duration", type=int, default=DEFAULT_DURATION)
    batch.add_argument("--output-dir", type=Path, default=Path("data/ace-step-covers"))
    batch.add_argument("--remote-output-dir", default=DEFAULT_OUTPUT_DIR)
    batch.add_argument("--recursive", action="store_true")
    batch.add_argument("--resume", action="store_true")
    batch.add_argument("--sleep-between", type=float, default=5.0)
    batch.set_defaults(func=run_ace_step_batch)


# ── Helpers ───────────────────────────────────────────────────────────


def _slugify(text: str) -> str:
    import re

    value = re.sub(r"[^A-Za-z0-9]+", "-", text.strip()).strip("-")
    return value or "untitled"


def _iter_audio_files(folder: Path, recursive: bool) -> list[Path]:
    search = folder.rglob("*") if recursive else folder.glob("*")
    return sorted(p for p in search if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS)


def _ssh_cmd(cmd: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    """Run a command on gpu-dev-3 via SSH."""
    return subprocess.run(
        ["ssh", ACE_STEP_HOST, cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _rsync_to_remote(local: Path, remote_dir: str) -> str:
    """Rsync a local file to gpu-dev-3. Returns remote path."""
    _ssh_cmd(f"mkdir -p {remote_dir}")
    remote_path = f"{remote_dir}/{local.name}"
    subprocess.run(
        ["rsync", "-az", str(local), f"{ACE_STEP_HOST}:{remote_path}"],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return remote_path


def _rsync_from_remote(remote_path: str, local_dir: Path) -> Path:
    """Rsync a file or directory from gpu-dev-3 to local. Returns local path."""
    local_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["rsync", "-az", f"{ACE_STEP_HOST}:{remote_path}", str(local_dir) + "/"],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return local_dir / Path(remote_path).name


def _resolve_lyrics(lyrics: str, lyrics_file: Path | None, sidecar: Path | None) -> str:
    """Resolve lyrics from argument, file flag, or sidecar .lyrics.txt."""
    if lyrics:
        return lyrics
    if lyrics_file and lyrics_file.exists():
        return lyrics_file.read_text(encoding="utf-8").strip()
    if sidecar and sidecar.exists():
        return sidecar.read_text(encoding="utf-8").strip()
    return ""


# ── Core Cover Function ──────────────────────────────────────────────


def _run_ace_step_cover(
    *,
    input_path: Path,
    output_dir: Path,
    tags: str,
    lyrics: str,
    noise_strength: float,
    duration: int,
    title: str,
    remote_output_dir: str,
) -> dict[str, Any]:
    """Run a single ACE Step cover via SSH to gpu-dev-3.

    Returns dict with input, remote command, output path, etc.
    """
    title = title or input_path.stem
    slug = _slugify(title)

    # Sync source to gpu-dev-3
    remote_audio = _rsync_to_remote(input_path, ACE_STEP_REMOTE_STAGING)

    # Build the generate command
    lyrics_arg = ""
    if lyrics:
        # Escape single quotes in lyrics for shell
        escaped = lyrics.replace("'", "'\\''")
        lyrics_arg = f"--lyrics '{escaped}'"

    gen_cmd = (
        f"cd {ACE_STEP_DIR} && "
        f"LD_LIBRARY_PATH={ACE_STEP_LD_LIBRARY_PATH} "
        f"{ACE_STEP_VENV_PYTHON} generate_cli.py "
        f"--tags '{tags}' "
        f"{lyrics_arg} "
        f"--ref-audio {remote_audio} "
        f"--cover-noise-strength {noise_strength} "
        f"--duration {duration} "
        f"--title '{slug}' "
        f"--output-dir {remote_output_dir}"
    )

    print(f"[ace-step] Generating cover for {input_path.name} (noise={noise_strength})...")
    result = _ssh_cmd(gen_cmd, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(
            f"ACE Step generation failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    # Sync output back — ACE Step writes to <output-dir>/<title>/
    remote_result_dir = f"{remote_output_dir}/{slug}"
    local_out = output_dir / slug
    _rsync_from_remote(remote_result_dir, local_out.parent)

    return {
        "input": str(input_path),
        "title": title,
        "slug": slug,
        "noise_strength": noise_strength,
        "tags": tags,
        "remote_cmd": gen_cmd,
        "output_dir": str(local_out),
        "stdout": result.stdout.strip(),
    }


# ── CLI Handlers ─────────────────────────────────────────────────────


def run_ace_step_cover(args: argparse.Namespace) -> int:
    input_path = args.input.resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input audio not found: {input_path}")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    sidecar = input_path.with_suffix(".lyrics.txt")
    lyrics = _resolve_lyrics(args.lyrics, args.lyrics_file, sidecar)

    result = _run_ace_step_cover(
        input_path=input_path,
        output_dir=output_dir,
        tags=args.tags,
        lyrics=lyrics,
        noise_strength=args.noise_strength,
        duration=args.duration,
        title=args.title,
        remote_output_dir=args.remote_output_dir,
    )
    print(json.dumps(result, indent=2))
    return 0


def run_ace_step_batch(args: argparse.Namespace) -> int:
    input_dir = args.input_dir.resolve()
    files = _iter_audio_files(input_dir, recursive=args.recursive)
    if not files:
        raise FileNotFoundError(f"No supported audio files found in {input_dir}")

    output_root = args.output_dir.resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    success_count = 0

    for input_path in files:
        slug = _slugify(input_path.stem)
        job_dir = output_root / slug
        done_marker = job_dir / ".done"

        if args.resume and done_marker.exists():
            records.append({"input": str(input_path), "status": "skipped"})
            print(f"[ace-step] Skipping {input_path.name} (already done)")
            continue

        job_dir.mkdir(parents=True, exist_ok=True)
        sidecar = input_path.with_suffix(".lyrics.txt")
        lyrics = _resolve_lyrics(args.lyrics, None, sidecar)

        try:
            result = _run_ace_step_cover(
                input_path=input_path,
                output_dir=output_root,
                tags=args.tags,
                lyrics=lyrics,
                noise_strength=args.noise_strength,
                duration=args.duration,
                title=input_path.stem,
                remote_output_dir=args.remote_output_dir,
            )
            done_marker.write_text("ok\n", encoding="utf-8")
            records.append({"input": str(input_path), "status": "complete", **result})
            success_count += 1
        except Exception as exc:
            records.append({
                "input": str(input_path),
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"[ace-step] Error processing {input_path.name}: {exc}")

        if args.sleep_between > 0:
            time.sleep(args.sleep_between)

    manifest = {
        "mode": "ace-step-batch",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "input_dir": str(input_dir),
        "output_dir": str(output_root),
        "records": records,
        "summary": {
            "total": len(files),
            "succeeded": success_count,
            "failed": len([r for r in records if r["status"] == "error"]),
            "skipped": len([r for r in records if r["status"] == "skipped"]),
        },
    }
    manifest_path = output_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"output_dir": str(output_root), "manifest": str(manifest_path), "succeeded": success_count}))
    return 0 if success_count else 4
