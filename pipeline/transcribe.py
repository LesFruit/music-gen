from __future__ import annotations

import wave
from pathlib import Path

import pretty_midi


class TranscriptionError(RuntimeError):
    pass


def _mock_transcription(audio_path: Path, output_midi: Path) -> None:
    with wave.open(str(audio_path), "rb") as handle:
        duration_s = max(handle.getnframes() / float(handle.getframerate() or 1), 1.0)

    midi = pretty_midi.PrettyMIDI(initial_tempo=120)
    piano = pretty_midi.Instrument(program=0, name="mock-piano")

    step = 0.5
    t = 0.0
    idx = 0
    pitches = [60, 64, 67, 72]
    while t < duration_s - 0.2:
        pitch = pitches[idx % len(pitches)]
        piano.notes.append(
            pretty_midi.Note(velocity=90, pitch=pitch, start=t, end=min(t + 0.35, duration_s))
        )
        t += step
        idx += 1

    midi.instruments.append(piano)
    output_midi.parent.mkdir(parents=True, exist_ok=True)
    midi.write(str(output_midi))


def _basicpitch_transcription(audio_path: Path, output_midi: Path) -> None:
    try:
        from basic_pitch.inference import predict
    except ImportError as exc:
        raise TranscriptionError(
            "BasicPitch is not installed. Install with: uv sync --extra transcribe"
        ) from exc

    try:
        _, midi_data, _ = predict(str(audio_path))
    except Exception as exc:  # noqa: BLE001
        raise TranscriptionError(f"BasicPitch inference failed: {exc}") from exc

    output_midi.parent.mkdir(parents=True, exist_ok=True)
    midi_data.write(str(output_midi))


def transcribe_audio(audio_path: Path, output_midi: Path, backend: str = "basicpitch") -> None:
    backend = backend.lower().strip()
    if backend == "mock":
        _mock_transcription(audio_path, output_midi)
        return
    if backend == "basicpitch":
        _basicpitch_transcription(audio_path, output_midi)
        return
    raise TranscriptionError(f"Unsupported transcription backend: {backend}")
