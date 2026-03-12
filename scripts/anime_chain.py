#!/usr/bin/env python3
"""Song -> ACE Step -> Suno chained cover pipeline.

Takes a source audio file, generates ACE Step covers (faithful + orchestral),
then uploads each to Suno for genre-varied covers.

Usage:
    coverctl anime-chain source.wav --slug blue-bird \
        --tags "anime, j-pop, naruto" --lyrics "habataitara modoranai to itte"

    coverctl anime-chain source.wav --slug immortal-king-s1 \
        --ace-variants faithful,orchestral \
        --suno-presets rock,orchestral,city-pop,ballad
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# ACE Step variant noise strengths
VARIANT_NOISE = {
    "faithful": 0.2,
    "orchestral": 0.4,
    "creative": 0.6,
}

# Suno preset tag mappings
SUNO_PRESET_TAGS = {
    "rock": "anime, j-rock, powerful, electric guitar, live drums, emotional, cinematic",
    "orchestral": "anime, orchestral, cinematic, strings, piano, emotional, soundtrack",
    "city-pop": "anime, city pop, glossy synths, bass groove, nostalgic, bright, polished",
    "ballad": "anime, emotional ballad, piano, strings, soaring vocal, heartfelt",
    "edm": "anime, electronic, EDM, synth, upbeat, energetic, remix",
    "lofi": "anime, lo-fi, chill, vinyl crackle, relaxing, mellow, study beats",
}

# ACE Step config
ACE_STEP_HOST = "100.116.10.41"
ACE_STEP_DIR = "/srv/ace-step"
LD_LIB = "/usr/lib/wsl/lib:/usr/lib/wsl/drivers"


def _run_ace_step(
    source_remote: str,
    tags: str,
    lyrics: str,
    noise: float,
    duration: int,
    title: str,
    output_dir: str,
) -> str:
    """Run ACE Step cover on gpu-dev-3 via SSH, return output path."""
    cmd = (
        f"cd {ACE_STEP_DIR} && "
        f"LD_LIBRARY_PATH={LD_LIB} "
        f".venv/bin/python generate_cli.py "
        f"--tags '{tags}' "
        f"--lyrics '{lyrics}' "
        f"--ref-audio '{source_remote}' "
        f"--cover-noise-strength {noise} "
        f"--audio-cover-strength 1.0 "
        f"--duration {duration} "
        f"--title '{title}' "
        f"--output-dir '{output_dir}'"
    )
    result = subprocess.run(
        ["ssh", ACE_STEP_HOST, cmd],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ACE Step failed: {result.stderr}")

    # Parse output path from "OUTPUT:<path>" line
    for line in result.stdout.splitlines():
        if line.startswith("OUTPUT:"):
            return line.split("OUTPUT:", 1)[1].strip()

    raise RuntimeError(f"No OUTPUT line in ACE Step output:\n{result.stdout[-500:]}")


def _scp_to_remote(local: Path, remote_path: str) -> None:
    subprocess.run(
        ["scp", str(local), f"{ACE_STEP_HOST}:{remote_path}"],
        check=True, capture_output=True, timeout=120,
    )


def _scp_from_remote(remote_path: str, local: Path) -> None:
    subprocess.run(
        ["scp", f"{ACE_STEP_HOST}:{remote_path}", str(local)],
        check=True, capture_output=True, timeout=120,
    )


async def _run_suno_cover(
    input_path: Path,
    tags: str,
    title: str,
    output_dir: Path,
    model: str,
    timeout: float,
    pre_download_wait: float,
) -> dict[str, Any]:
    """Run a single Suno cover job."""
    from coverctl.suno_jobs import _run_cover_job

    output_dir.mkdir(parents=True, exist_ok=True)
    return await _run_cover_job(
        input_path=input_path,
        output_dir=output_dir,
        prompt="",
        title=title,
        tags=tags,
        instrumental=False,
        model=model,
        timeout=timeout,
        poll_interval=5.0,
        pre_download_wait=pre_download_wait,
        wav=True,
    )


def run_chain(args: argparse.Namespace) -> int:
    source = args.source.resolve()
    if not source.exists():
        print(f"[chain] Source not found: {source}")
        return 1

    slug = args.slug
    output_root = args.output_dir.resolve() / slug
    ace_variants = [v.strip() for v in args.ace_variants.split(",")]
    suno_presets = [p.strip() for p in args.suno_presets.split(",")]
    tags = args.tags
    lyrics = args.lyrics
    duration = args.duration

    print(f"[chain] Song -> ACE Step -> Suno pipeline")
    print(f"[chain] Source: {source}")
    print(f"[chain] Slug: {slug}")
    print(f"[chain] ACE variants: {ace_variants}")
    print(f"[chain] Suno presets: {suno_presets}")
    print()

    # Create output structure
    (output_root / "source").mkdir(parents=True, exist_ok=True)
    source_dest = output_root / "source" / "original.wav"
    if not source_dest.exists():
        shutil.copy2(source, source_dest)

    # ── Phase 1: Upload source to gpu-dev-3 ──
    remote_staging = f"/tmp/ace-step-anime/sources/{slug}.wav"
    print(f"[chain] Uploading source to gpu-dev-3:{remote_staging}")
    subprocess.run(
        ["ssh", ACE_STEP_HOST, f"mkdir -p /tmp/ace-step-anime/sources /tmp/ace-step-anime/output"],
        check=True, capture_output=True,
    )
    _scp_to_remote(source, remote_staging)

    # ── Phase 2: ACE Step covers ──
    ace_outputs: dict[str, Path] = {}
    for variant in ace_variants:
        noise = VARIANT_NOISE.get(variant)
        if noise is None:
            print(f"[chain] Unknown variant '{variant}', skipping")
            continue

        variant_dir = output_root / "ace-step" / variant
        variant_dir.mkdir(parents=True, exist_ok=True)
        done_marker = variant_dir / ".done"

        if done_marker.exists():
            # Find existing cover
            existing = list(variant_dir.glob("*.wav"))
            if existing:
                ace_outputs[variant] = existing[0]
                print(f"[chain] ACE Step {variant}: skipped (already done)")
                continue

        # Use orchestral tags for orchestral variant
        ace_tags = tags
        if variant == "orchestral":
            ace_tags = "anime, orchestral, cinematic, strings, piano, brass, emotional, soundtrack, epic"

        title = f"{slug}-{variant}"
        print(f"[chain] ACE Step {variant} (noise={noise})...")

        try:
            remote_output = _run_ace_step(
                source_remote=remote_staging,
                tags=ace_tags,
                lyrics=lyrics,
                noise=noise,
                duration=duration,
                title=title,
                output_dir="/tmp/ace-step-anime/output",
            )
            local_output = variant_dir / Path(remote_output).name
            _scp_from_remote(remote_output, local_output)
            done_marker.write_text("ok\n")
            ace_outputs[variant] = local_output
            print(f"[chain] ACE Step {variant}: {local_output.name}")
        except Exception as exc:
            print(f"[chain] ACE Step {variant} FAILED: {exc}")

    if not ace_outputs:
        print("[chain] No ACE Step covers produced, cannot continue to Suno")
        return 1

    # ── Phase 3: Suno covers from ACE Step outputs ──
    suno_results: list[dict[str, Any]] = []

    # Use the orchestral ACE Step output for Suno if available, else faithful
    suno_source_variant = "orchestral" if "orchestral" in ace_outputs else list(ace_outputs.keys())[0]
    suno_source = ace_outputs[suno_source_variant]

    print(f"\n[chain] Suno covers from ACE Step {suno_source_variant}: {suno_source.name}")

    for preset in suno_presets:
        preset_tags = SUNO_PRESET_TAGS.get(preset)
        if preset_tags is None:
            print(f"[chain] Unknown Suno preset '{preset}', skipping")
            continue

        preset_dir = output_root / "suno" / preset
        done_marker = preset_dir / ".done"

        if done_marker.exists():
            print(f"[chain] Suno {preset}: skipped (already done)")
            continue

        cover_title = f"{slug} {preset.replace('-', ' ')} cover"
        print(f"[chain] Suno {preset}...")

        try:
            result = asyncio.run(_run_suno_cover(
                input_path=suno_source,
                tags=preset_tags,
                title=cover_title,
                output_dir=preset_dir,
                model=args.model,
                timeout=args.timeout,
                pre_download_wait=args.pre_download_wait,
            ))
            done_marker.write_text("ok\n")
            suno_results.append({"preset": preset, "status": "complete", **result})
            print(f"[chain] Suno {preset}: done")
        except Exception as exc:
            suno_results.append({"preset": preset, "status": "error", "error": str(exc)})
            print(f"[chain] Suno {preset} FAILED: {exc}")

    # ── Phase 4: Write manifest ──
    manifest = {
        "slug": slug,
        "source": str(source),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ace_step": {
            variant: str(path) for variant, path in ace_outputs.items()
        },
        "suno": suno_results,
        "summary": {
            "ace_variants": len(ace_outputs),
            "suno_complete": sum(1 for r in suno_results if r["status"] == "complete"),
            "suno_errors": sum(1 for r in suno_results if r["status"] == "error"),
        },
    }
    manifest_path = output_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # ── Summary ──
    print(f"\n{'='*60}")
    print(f"[chain] Complete: {slug}")
    print(f"{'='*60}")
    print(f"  ACE Step covers: {len(ace_outputs)}")
    print(f"  Suno covers:     {manifest['summary']['suno_complete']} complete, {manifest['summary']['suno_errors']} errors")
    print(f"  Output:          {output_root}")
    print(f"  Manifest:        {manifest_path}")

    return 0 if manifest["summary"]["suno_errors"] == 0 else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Song -> ACE Step -> Suno chain")
    parser.add_argument("source", type=Path)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--tags", default="anime, j-rock, cover")
    parser.add_argument("--lyrics", default="")
    parser.add_argument("--output-dir", type=Path, default=Path("data/anime-covers"))
    parser.add_argument("--duration", type=int, default=120)
    parser.add_argument("--ace-variants", default="faithful,orchestral")
    parser.add_argument("--suno-presets", default="rock,orchestral,city-pop,ballad")
    parser.add_argument("--model", default="chirp-crow")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--pre-download-wait", type=float, default=25.0)
    args = parser.parse_args(argv)
    return run_chain(args)


if __name__ == "__main__":
    sys.exit(main())
