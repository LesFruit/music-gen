from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pretty_midi

MEL_PROGRAM = 40  # Violin
PAD_PROGRAM = 48  # String Ensemble 1
BASS_PROGRAM = 42  # Cello


def arrange_orchestra(input_midi: Path, output_midi: Path) -> None:
    midi = pretty_midi.PrettyMIDI(str(input_midi))
    tempo = midi.estimate_tempo() or 120.0

    all_notes: list[pretty_midi.Note] = []
    for instrument in midi.instruments:
        all_notes.extend(instrument.notes)
    all_notes.sort(key=lambda n: (round(n.start, 3), n.pitch))

    by_start: dict[float, list[pretty_midi.Note]] = defaultdict(list)
    for note in all_notes:
        bucket_key = round(note.start, 2)
        by_start[bucket_key].append(note)

    melody = pretty_midi.Instrument(program=MEL_PROGRAM, name="melody")
    harmony = pretty_midi.Instrument(program=PAD_PROGRAM, name="harmony")
    bass = pretty_midi.Instrument(program=BASS_PROGRAM, name="bass")

    for _, group in sorted(by_start.items(), key=lambda item: item[0]):
        group = sorted(group, key=lambda n: n.pitch)
        lowest = group[0]
        highest = group[-1]

        bass.notes.append(
            pretty_midi.Note(
                velocity=max(lowest.velocity - 5, 40),
                pitch=max(28, min(lowest.pitch, 60)),
                start=lowest.start,
                end=lowest.end,
            )
        )

        melody.notes.append(
            pretty_midi.Note(
                velocity=min(highest.velocity + 5, 120),
                pitch=max(55, min(highest.pitch, 100)),
                start=highest.start,
                end=highest.end,
            )
        )

        if len(group) > 2:
            mids = group[1:-1]
        else:
            mids = group

        for mid in mids:
            harmony.notes.append(
                pretty_midi.Note(
                    velocity=max(mid.velocity - 15, 35),
                    pitch=max(45, min(mid.pitch, 84)),
                    start=mid.start,
                    end=max(mid.end, mid.start + 0.3),
                )
            )

    out = pretty_midi.PrettyMIDI(initial_tempo=tempo)
    out.instruments.extend([melody, harmony, bass])
    output_midi.parent.mkdir(parents=True, exist_ok=True)
    out.write(str(output_midi))
