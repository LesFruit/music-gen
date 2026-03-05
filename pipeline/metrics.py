from __future__ import annotations

from pathlib import Path

import numpy as np
import pretty_midi

SHORT_NOTE_MS_DEFAULT = 80.0


def _all_notes(midi: pretty_midi.PrettyMIDI) -> list[pretty_midi.Note]:
    notes: list[pretty_midi.Note] = []
    for instrument in midi.instruments:
        notes.extend(instrument.notes)
    return notes


def compute_midi_metrics(
    midi_path: Path, short_note_ms: float = SHORT_NOTE_MS_DEFAULT
) -> dict[str, float]:
    midi = pretty_midi.PrettyMIDI(str(midi_path))
    notes = _all_notes(midi)

    duration = max(midi.get_end_time(), 1e-6)
    note_count = len(notes)
    note_density = note_count / duration

    short_note_s = short_note_ms / 1000.0
    short_count = sum(1 for n in notes if (n.end - n.start) < short_note_s)
    short_ratio = short_count / note_count if note_count else 0.0

    if note_count:
        pitches = [n.pitch for n in notes]
        coverage = (max(pitches) - min(pitches)) / 87.0
    else:
        coverage = 0.0

    tempo = midi.estimate_tempo() or 120.0

    onsets = sorted(n.start for n in notes)
    if len(onsets) >= 3:
        iois = np.diff(onsets)
        ioi_mean = float(np.mean(iois)) if np.mean(iois) else 0.0
        tempo_stability = float(np.std(iois) / ioi_mean) if ioi_mean > 0 else 0.0
    else:
        tempo_stability = 0.0

    return {
        "duration_s": float(duration),
        "note_count": float(note_count),
        "note_density": float(note_density),
        "short_note_ratio": float(short_ratio),
        "pitch_range_coverage": float(max(0.0, min(coverage, 1.0))),
        "tempo_estimate_bpm": float(tempo),
        "tempo_stability": float(tempo_stability),
    }
