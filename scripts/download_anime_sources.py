#!/usr/bin/env python3
"""Download anime source audio from anime_songs.json using yt-dlp.

Usage:
    python scripts/download_anime_sources.py [--songs-file data/anime-sources/anime_songs.json]
    python scripts/download_anime_sources.py --slug gurenge --slug blue-bird
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

YT_DLP = "yt-dlp"
DEFAULT_SONGS_FILE = Path(__file__).resolve().parent.parent / "data" / "anime-sources" / "anime_songs.json"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "anime-sources"


def _download_wav(url: str, output_path: Path, duration_s: int = 120) -> bool:
    """Download audio as WAV via yt-dlp, trimming to duration_s."""
    output_template = str(output_path.with_suffix(""))
    cmd = [
        YT_DLP,
        "--extract-audio",
        "--audio-format", "wav",
        "--audio-quality", "0",
        "--output", output_template + ".%(ext)s",
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        url,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=600)
    except subprocess.CalledProcessError as exc:
        print(f"  yt-dlp failed: {exc.stderr.strip()}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print(f"  yt-dlp not found. Install with: pip install yt-dlp", file=sys.stderr)
        return False

    # yt-dlp may produce the file with its own extension choice; find it
    wav_path = output_path.with_suffix(".wav")
    if not wav_path.exists():
        # Check if yt-dlp created a file with the template name
        for ext in [".wav", ".mp3", ".m4a", ".webm", ".opus"]:
            candidate = Path(output_template + ext)
            if candidate.exists():
                # Convert to WAV
                _convert_to_wav(candidate, wav_path)
                candidate.unlink()
                break

    if not wav_path.exists():
        print(f"  Download succeeded but WAV file not found at {wav_path}", file=sys.stderr)
        return False

    # Trim to target duration
    if duration_s > 0:
        _trim_wav(wav_path, duration_s)

    return True


def _convert_to_wav(input_path: Path, output_path: Path) -> None:
    """Convert any audio file to WAV using ffmpeg."""
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(input_path), "-ar", "44100", "-ac", "2", str(output_path)],
        check=True,
        capture_output=True,
        timeout=120,
    )


def _trim_wav(wav_path: Path, max_seconds: int) -> None:
    """Trim WAV to max_seconds using ffmpeg (in-place)."""
    tmp = wav_path.with_suffix(".tmp.wav")
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(wav_path),
            "-t", str(max_seconds),
            "-ar", "44100", "-ac", "2",
            str(tmp),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    tmp.replace(wav_path)


def _save_lyrics(slug: str, lyrics: str, output_dir: Path) -> None:
    """Save lyrics to a sidecar .lyrics.txt file."""
    if not lyrics:
        return
    lyrics_path = output_dir / f"{slug}.lyrics.txt"
    lyrics_path.write_text(lyrics, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download anime source audio for cover pipeline")
    parser.add_argument("--songs-file", type=Path, default=DEFAULT_SONGS_FILE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--slug", action="append", default=None, help="Only download specific slugs")
    args = parser.parse_args(argv)

    songs_file = args.songs_file.resolve()
    if not songs_file.exists():
        print(f"Songs file not found: {songs_file}", file=sys.stderr)
        return 1

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    songs = json.loads(songs_file.read_text(encoding="utf-8"))
    filter_slugs = set(args.slug) if args.slug else None

    success = 0
    skipped = 0
    failed = 0

    for song in songs:
        slug = song["slug"]
        if filter_slugs and slug not in filter_slugs:
            continue

        wav_path = output_dir / f"{slug}.wav"
        if wav_path.exists():
            print(f"[skip] {slug} — already exists")
            skipped += 1
            continue

        url = song.get("url", "")
        if not url:
            print(f"[skip] {slug} — no URL provided, place {slug}.wav manually")
            skipped += 1
            # Still save lyrics if available
            _save_lyrics(slug, song.get("lyrics_romaji", ""), output_dir)
            continue

        duration = song.get("duration_s", 120)
        print(f"[download] {slug} — {song['title']} by {song['artist']}...")

        if _download_wav(url, wav_path, duration):
            print(f"  -> saved {wav_path}")
            success += 1
        else:
            print(f"  -> FAILED")
            failed += 1

        # Save lyrics sidecar
        _save_lyrics(slug, song.get("lyrics_romaji", ""), output_dir)

    print(f"\nDone: {success} downloaded, {skipped} skipped, {failed} failed")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
