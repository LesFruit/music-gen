import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_sine_wav(path: Path, duration_s: float = 2.0, sr: int = 44100) -> None:
    t = np.linspace(0.0, duration_s, int(sr * duration_s), endpoint=False)
    wave = 0.2 * np.sin(2 * np.pi * 440.0 * t)
    pcm = (wave * 32767).astype(np.int16)
    wavfile.write(str(path), sr, pcm)


def test_smoke_pipeline_piano(tmp_path: Path) -> None:
    in_wav = tmp_path / "sample.wav"
    out_root = tmp_path / "out"
    _write_sine_wav(in_wav)

    cmd = [
        sys.executable,
        "-m",
        "coverctl",
        "run",
        str(in_wav),
        "--style",
        "piano",
        "--job-id",
        "smoke-piano",
        "--output-dir",
        str(out_root),
        "--transcriber",
        "mock",
    ]
    subprocess.run(cmd, cwd=REPO_ROOT, check=True)

    job_dir = out_root / "smoke-piano"
    assert (job_dir / "transcription.mid").exists()
    assert (job_dir / "cover_piano.mid").exists()
    assert (job_dir / "cover_piano.wav").exists()
    assert (job_dir / "manifest.json").exists()

    manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["job_id"] == "smoke-piano"
    assert "cover_piano_wav" in manifest["artifacts"]


def test_smoke_pipeline_orchestra(tmp_path: Path) -> None:
    in_wav = tmp_path / "sample.wav"
    out_root = tmp_path / "out"
    _write_sine_wav(in_wav)

    cmd = [
        sys.executable,
        "-m",
        "coverctl",
        "run",
        str(in_wav),
        "--style",
        "orchestra",
        "--job-id",
        "smoke-orchestra",
        "--output-dir",
        str(out_root),
        "--transcriber",
        "mock",
    ]
    subprocess.run(cmd, cwd=REPO_ROOT, check=True)

    job_dir = out_root / "smoke-orchestra"
    assert (job_dir / "transcription.mid").exists()
    assert (job_dir / "cover_orchestra.mid").exists()
    assert (job_dir / "cover_orchestra.wav").exists()
    assert (job_dir / "manifest.json").exists()
