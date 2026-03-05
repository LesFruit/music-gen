from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pretty_midi

from pipeline.midi_utils import safe_estimate_tempo

PIANO_LOW = 21
PIANO_HIGH = 108


@dataclass(slots=True)
class MidiCleanConfig:
    min_note_ms: float = 60.0
    quantize_subdivision: int = 4  # 4 => 16th note grid


def _clamp_pitch(pitch: int) -> int:
    if pitch < PIANO_LOW:
        return PIANO_LOW
    if pitch > PIANO_HIGH:
        return PIANO_HIGH
    return pitch


def _quantize(value: float, grid: float) -> float:
    if grid <= 0:
        return value
    return round(value / grid) * grid


def _dedupe_overlaps(notes: list[pretty_midi.Note]) -> list[pretty_midi.Note]:
    by_pitch: dict[int, list[pretty_midi.Note]] = {}
    for note in sorted(notes, key=lambda n: (n.pitch, n.start, n.end)):
        bucket = by_pitch.setdefault(note.pitch, [])
        if not bucket:
            bucket.append(note)
            continue
        last = bucket[-1]
        if note.start < last.end:
            if note.end > last.end:
                last.end = note.end
            if note.velocity > last.velocity:
                last.velocity = note.velocity
            continue
        bucket.append(note)

    deduped: list[pretty_midi.Note] = []
    for bucket in by_pitch.values():
        deduped.extend(bucket)
    return sorted(deduped, key=lambda n: (n.start, n.pitch, n.end))


def clean_midi(input_midi: Path, output_midi: Path, config: MidiCleanConfig | None = None) -> None:
    cfg = config or MidiCleanConfig()

    midi = pretty_midi.PrettyMIDI(str(input_midi))
    tempo = safe_estimate_tempo(midi)
    seconds_per_beat = 60.0 / max(tempo, 1.0)
    grid = seconds_per_beat / max(cfg.quantize_subdivision, 1)
    min_note_s = cfg.min_note_ms / 1000.0

    clean = pretty_midi.PrettyMIDI(initial_tempo=tempo)

    for instrument in midi.instruments:
        target = pretty_midi.Instrument(program=0, is_drum=instrument.is_drum, name=instrument.name)
        candidate_notes: list[pretty_midi.Note] = []

        for note in instrument.notes:
            duration = note.end - note.start
            if duration < min_note_s:
                continue

            start = max(0.0, _quantize(note.start, grid))
            end = max(start + min_note_s, _quantize(note.end, grid))
            pitch = _clamp_pitch(note.pitch)

            candidate_notes.append(
                pretty_midi.Note(
                    velocity=note.velocity,
                    pitch=pitch,
                    start=start,
                    end=end,
                )
            )

        target.notes = _dedupe_overlaps(candidate_notes)
        clean.instruments.append(target)

    output_midi.parent.mkdir(parents=True, exist_ok=True)
    clean.write(str(output_midi))
