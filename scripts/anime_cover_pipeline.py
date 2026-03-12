#!/usr/bin/env python3
"""Unified anime cover generation pipeline.

Orchestrates ACE Step (gpu-dev-3) and Suno to produce multiple cover
variants of anime songs.

Usage:
    # Cover all songs in a folder with both engines
    python scripts/anime_cover_pipeline.py /path/to/songs/ --output-dir data/anime-covers

    # Cover specific files
    python scripts/anime_cover_pipeline.py song1.wav song2.wav --output-dir data/anime-covers

    # Only ACE Step
    python scripts/anime_cover_pipeline.py /path/to/songs/ --engine ace-step

    # Only Suno
    python scripts/anime_cover_pipeline.py /path/to/songs/ --engine suno

    # Download from anime_songs.json first, then cover
    python scripts/anime_cover_pipeline.py --from-list data/anime-sources/anime_songs.json --output-dir data/anime-covers
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

# ── Constants ─────────────────────────────────────────────────────────

SUPPORTED_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".mp4"}

ACE_STEP_VARIANTS = {
    "faithful": 0.2,
    "creative": 0.5,
}

SUNO_PRESETS = {
    "anime-rock": "anime, j-rock, powerful, electric guitar, live drums, emotional, cinematic",
    "anime-orchestral": "anime, orchestral, cinematic, strings, piano, emotional, soundtrack",
    "anime-city-pop": "anime, city pop, glossy synths, bass groove, nostalgic, bright, polished",
    "anime-ballad": "anime, emotional ballad, piano, strings, soaring vocal, heartfelt",
}

DEFAULT_DURATION = 60
# Suno v5 model — always use chirp-crow (v5), never older models.
DEFAULT_SUNO_MODEL = "chirp-crow"


# ── Helpers ───────────────────────────────────────────────────────────


def _slugify(text: str) -> str:
    import re

    value = re.sub(r"[^A-Za-z0-9]+", "-", text.strip()).strip("-")
    return value.lower() or "untitled"


def _iter_audio_files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.glob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS)


def _load_song_metadata(songs_file: Path) -> dict[str, dict[str, Any]]:
    """Load anime_songs.json into a dict keyed by slug."""
    songs = json.loads(songs_file.read_text(encoding="utf-8"))
    return {s["slug"]: s for s in songs}


def _resolve_lyrics(input_path: Path) -> str:
    """Look for a .lyrics.txt sidecar next to the input file."""
    sidecar = input_path.with_suffix(".lyrics.txt")
    if sidecar.exists():
        return sidecar.read_text(encoding="utf-8").strip()
    return ""


# ── Phase: Download ──────────────────────────────────────────────────


def _download_from_list(songs_file: Path, source_dir: Path) -> list[Path]:
    """Download songs from anime_songs.json, return list of local WAV paths."""
    from scripts.download_anime_sources import main as download_main

    download_main(["--songs-file", str(songs_file), "--output-dir", str(source_dir)])

    return _iter_audio_files(source_dir)


# ── Phase: ACE Step Covers ───────────────────────────────────────────


def _run_ace_step_phase(
    files: list[Path],
    output_root: Path,
    metadata: dict[str, dict[str, Any]],
    duration: int,
) -> list[dict[str, Any]]:
    """Generate ACE Step covers for each file (faithful + creative variants)."""
    from coverctl.ace_step_jobs import _run_ace_step_cover

    results: list[dict[str, Any]] = []

    for input_path in files:
        slug = _slugify(input_path.stem)
        song_meta = metadata.get(slug, {})
        tags = song_meta.get("tags", "anime, j-rock, cover")
        lyrics = _resolve_lyrics(input_path) or song_meta.get("lyrics_romaji", "")
        song_dir = output_root / slug / "ace-step"

        for variant_name, noise in ACE_STEP_VARIANTS.items():
            variant_dir = song_dir / variant_name
            done_marker = variant_dir / ".done"

            if done_marker.exists():
                print(f"[ace-step] Skipping {slug}/{variant_name} (already done)")
                results.append({
                    "slug": slug, "engine": "ace-step", "variant": variant_name, "status": "skipped",
                })
                continue

            variant_dir.mkdir(parents=True, exist_ok=True)
            try:
                result = _run_ace_step_cover(
                    input_path=input_path,
                    output_dir=variant_dir,
                    tags=tags,
                    lyrics=lyrics,
                    noise_strength=noise,
                    duration=duration,
                    title=f"{slug}-{variant_name}",
                    remote_output_dir=f"/tmp/ace-step-anime/{slug}/{variant_name}",
                )
                done_marker.write_text("ok\n", encoding="utf-8")
                results.append({
                    "slug": slug, "engine": "ace-step", "variant": variant_name,
                    "status": "complete", **result,
                })
            except Exception as exc:
                results.append({
                    "slug": slug, "engine": "ace-step", "variant": variant_name,
                    "status": "error", "error": f"{type(exc).__name__}: {exc}",
                })
                print(f"[ace-step] Error: {slug}/{variant_name}: {exc}")

    return results


# ── Phase: Suno Covers ───────────────────────────────────────────────


async def _run_suno_phase(
    files: list[Path],
    output_root: Path,
    metadata: dict[str, dict[str, Any]],
    model: str,
    timeout: float,
    poll_interval: float,
    pre_download_wait: float,
) -> list[dict[str, Any]]:
    """Generate Suno covers for each file across all presets."""
    from coverctl.suno_jobs import _run_cover_job

    results: list[dict[str, Any]] = []

    for input_path in files:
        slug = _slugify(input_path.stem)
        song_meta = metadata.get(slug, {})

        for preset_name, preset_tags in SUNO_PRESETS.items():
            preset_dir = output_root / slug / "suno" / preset_name
            done_marker = preset_dir / ".done"

            if done_marker.exists():
                print(f"[suno] Skipping {slug}/{preset_name} (already done)")
                results.append({
                    "slug": slug, "engine": "suno", "preset": preset_name, "status": "skipped",
                })
                continue

            preset_dir.mkdir(parents=True, exist_ok=True)
            title = song_meta.get("title", input_path.stem) + f" {preset_name.replace('-', ' ')}"

            max_retries = 2
            for attempt in range(max_retries + 1):
                try:
                    result = await _run_cover_job(
                        input_path=input_path,
                        output_dir=preset_dir,
                        prompt="",
                        title=title,
                        tags=preset_tags,
                        instrumental=False,
                        model=model,
                        timeout=timeout,
                        poll_interval=poll_interval,
                        pre_download_wait=pre_download_wait,
                        wav=True,
                    )
                    done_marker.write_text("ok\n", encoding="utf-8")
                    results.append({
                        "slug": slug, "engine": "suno", "preset": preset_name,
                        "status": "complete", **result,
                    })
                    break
                except Exception as exc:
                    err_str = str(exc).lower()
                    is_captcha_or_auth = any(
                        kw in err_str
                        for kw in ("captcha", "token validation", "unauthorized", "403", "401")
                    )
                    if is_captcha_or_auth and attempt < max_retries:
                        print(f"[suno] Auth/captcha error for {slug}/{preset_name}, attempting solve (retry {attempt + 1})...")
                        try:
                            await _solve_captcha_and_refresh()
                        except Exception as solve_exc:
                            print(f"[suno] Captcha solve failed: {solve_exc}")
                        continue

                    results.append({
                        "slug": slug, "engine": "suno", "preset": preset_name,
                        "status": "error", "error": f"{type(exc).__name__}: {exc}",
                    })
                    print(f"[suno] Error: {slug}/{preset_name}: {exc}")
                    break

    return results


async def _solve_captcha_and_refresh() -> None:
    """Attempt captcha solve and refresh tokens."""
    from suno_wrapper.captcha_solver import CaptchaSolver
    from suno_wrapper.env_util import reload_env_to_os, save_token

    solver = CaptchaSolver()
    result = await solver.solve()
    if result.success and result.token:
        save_token(result.token)
        reload_env_to_os()
        print(f"[suno] Captcha solved via {result.method}, token refreshed")
    else:
        raise RuntimeError(f"All captcha solve methods failed: {result.error}")


# ── Phase: Copy Sources ──────────────────────────────────────────────


def _copy_sources(files: list[Path], output_root: Path) -> None:
    """Copy source files into the output directory structure."""
    for input_path in files:
        slug = _slugify(input_path.stem)
        dest = output_root / slug / "source.wav"
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(input_path, dest)


# ── Phase: Write Manifests ───────────────────────────────────────────


def _write_song_manifests(
    files: list[Path],
    output_root: Path,
    ace_results: list[dict[str, Any]],
    suno_results: list[dict[str, Any]],
) -> None:
    """Write per-song manifest.json files."""
    all_results = ace_results + suno_results
    by_slug: dict[str, list[dict[str, Any]]] = {}
    for r in all_results:
        by_slug.setdefault(r["slug"], []).append(r)

    for input_path in files:
        slug = _slugify(input_path.stem)
        song_dir = output_root / slug
        manifest = {
            "slug": slug,
            "source": str(input_path),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "results": by_slug.get(slug, []),
        }
        (song_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )


# ── Main Orchestrator ────────────────────────────────────────────────


def run_pipeline(args: argparse.Namespace) -> int:
    engines = set(args.engine) if args.engine else {"ace-step", "suno"}

    # Resolve input files
    if args.from_list:
        songs_file = Path(args.from_list).resolve()
        source_dir = songs_file.parent
        metadata = _load_song_metadata(songs_file)
        print(f"[pipeline] Downloading {len(metadata)} songs from {songs_file.name}...")
        files = _download_from_list(songs_file, source_dir)
    else:
        inputs = [Path(p).resolve() for p in args.inputs]
        files = []
        metadata = {}
        for p in inputs:
            if p.is_dir():
                files.extend(_iter_audio_files(p))
            elif p.is_file():
                files.append(p)
            else:
                print(f"[pipeline] Warning: {p} not found, skipping")

        # Try to load metadata from default location
        default_meta = Path(__file__).resolve().parent.parent / "data" / "anime-sources" / "anime_songs.json"
        if default_meta.exists():
            metadata = _load_song_metadata(default_meta)

    if not files:
        print("[pipeline] No audio files found.")
        return 1

    output_root = Path(args.output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    print(f"[pipeline] Processing {len(files)} files → {output_root}")
    print(f"[pipeline] Engines: {', '.join(sorted(engines))}")

    # Copy sources
    _copy_sources(files, output_root)

    ace_results: list[dict[str, Any]] = []
    suno_results: list[dict[str, Any]] = []

    # ACE Step phase (sequential, SSH-based)
    if "ace-step" in engines:
        print(f"\n{'='*60}")
        print("[pipeline] Phase: ACE Step covers")
        print(f"{'='*60}")
        ace_results = _run_ace_step_phase(files, output_root, metadata, args.duration)

    # Suno phase (async)
    if "suno" in engines:
        print(f"\n{'='*60}")
        print("[pipeline] Phase: Suno covers")
        print(f"{'='*60}")
        suno_results = asyncio.run(
            _run_suno_phase(
                files, output_root, metadata,
                model=args.model,
                timeout=args.timeout,
                poll_interval=args.poll_interval,
                pre_download_wait=args.pre_download_wait,
            )
        )

    # Write manifests
    _write_song_manifests(files, output_root, ace_results, suno_results)

    # Summary
    all_results = ace_results + suno_results
    completed = [r for r in all_results if r["status"] == "complete"]
    errors = [r for r in all_results if r["status"] == "error"]
    skipped = [r for r in all_results if r["status"] == "skipped"]

    print(f"\n{'='*60}")
    print("[pipeline] Summary")
    print(f"{'='*60}")
    print(f"  Songs:     {len(files)}")
    print(f"  Completed: {len(completed)}")
    print(f"  Skipped:   {len(skipped)}")
    print(f"  Errors:    {len(errors)}")

    if errors:
        print("\n  Errors:")
        for e in errors:
            print(f"    - {e.get('slug', '?')}/{e.get('variant', e.get('preset', '?'))}: {e.get('error', '?')}")

    # Write top-level manifest
    pipeline_manifest = {
        "mode": "anime-cover-pipeline",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "engines": sorted(engines),
        "output_dir": str(output_root),
        "summary": {
            "total_songs": len(files),
            "total_jobs": len(all_results),
            "completed": len(completed),
            "skipped": len(skipped),
            "errors": len(errors),
        },
        "results": all_results,
    }
    manifest_path = output_root / "pipeline_manifest.json"
    manifest_path.write_text(json.dumps(pipeline_manifest, indent=2), encoding="utf-8")
    print(f"\n  Manifest: {manifest_path}")

    return 0 if not errors else 2


# ── CLI ───────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Unified anime cover generation pipeline",
    )
    parser.add_argument(
        "inputs", nargs="*", default=[],
        help="Audio files or directories to process",
    )
    parser.add_argument(
        "--from-list", default=None,
        help="Path to anime_songs.json — downloads sources first",
    )
    parser.add_argument("--output-dir", default="data/anime-covers")
    parser.add_argument(
        "--engine", action="append", choices=["ace-step", "suno"],
        help="Which engines to use (default: both). Can be repeated.",
    )
    parser.add_argument("--duration", type=int, default=DEFAULT_DURATION)
    parser.add_argument("--model", default=DEFAULT_SUNO_MODEL)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--pre-download-wait", type=float, default=20.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.inputs and not args.from_list:
        parser.error("Provide input files/directories or --from-list")

    return run_pipeline(args)


if __name__ == "__main__":
    sys.exit(main())
