from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pretty_midi
from scipy.io import wavfile


class RenderError(RuntimeError):
    pass


def _render_with_fluidsynth(
    midi_path: Path,
    wav_path: Path,
    soundfont_path: Path,
    sample_rate: int,
) -> None:
    bin_path = shutil.which("fluidsynth")
    if not bin_path:
        raise RenderError("fluidsynth binary not found")
    if not soundfont_path.exists():
        raise RenderError(f"soundfont not found: {soundfont_path}")

    cmd = [
        bin_path,
        "-ni",
        str(soundfont_path),
        str(midi_path),
        "-F",
        str(wav_path),
        "-r",
        str(sample_rate),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RenderError(proc.stderr.strip() or "fluidsynth render failed")


def _render_with_sine(midi_path: Path, wav_path: Path, sample_rate: int) -> None:
    midi = pretty_midi.PrettyMIDI(str(midi_path))
    audio = midi.synthesize(fs=sample_rate)
    audio = np.clip(audio, -1.0, 1.0)
    pcm = (audio * 32767).astype(np.int16)
    wavfile.write(str(wav_path), sample_rate, pcm)


def render_midi_to_wav(
    midi_path: Path,
    wav_path: Path,
    sample_rate: int = 44100,
    soundfont_path: Path | None = None,
) -> str:
    wav_path.parent.mkdir(parents=True, exist_ok=True)

    if soundfont_path is not None:
        try:
            _render_with_fluidsynth(midi_path, wav_path, soundfont_path, sample_rate)
            return "fluidsynth"
        except RenderError:
            pass

    _render_with_sine(midi_path, wav_path, sample_rate)
    return "sine"
