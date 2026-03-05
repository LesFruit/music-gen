from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path


class AudioNormalizeError(RuntimeError):
    pass


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def probe_wav(path: Path) -> tuple[float, int]:
    with wave.open(str(path), "rb") as handle:
        frames = handle.getnframes()
        sr = handle.getframerate()
    duration = frames / float(sr or 1)
    return duration, sr


def normalize_audio(
    input_path: Path,
    output_path: Path,
    sample_rate: int = 44100,
    channels: int = 2,
) -> tuple[float, int]:
    ensure_parent(output_path)

    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        cmd = [
            ffmpeg_path,
            "-y",
            "-i",
            str(input_path),
            "-ar",
            str(sample_rate),
            "-ac",
            str(channels),
            str(output_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise AudioNormalizeError(f"ffmpeg normalize failed: {proc.stderr.strip()}")
    else:
        if input_path.suffix.lower() != ".wav":
            raise AudioNormalizeError("ffmpeg is required for non-wav input")
        shutil.copy2(input_path, output_path)

    try:
        return probe_wav(output_path)
    except (wave.Error, FileNotFoundError) as exc:
        raise AudioNormalizeError(f"normalized wav is unreadable: {output_path}") from exc
