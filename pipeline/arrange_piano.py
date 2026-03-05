from __future__ import annotations

from pathlib import Path

import pretty_midi


def arrange_piano(input_midi: Path, output_midi: Path) -> None:
    midi = pretty_midi.PrettyMIDI(str(input_midi))

    arranged = pretty_midi.PrettyMIDI(initial_tempo=midi.estimate_tempo() or 120.0)
    piano = pretty_midi.Instrument(program=0, name="piano")
    for instrument in midi.instruments:
        piano.notes.extend(instrument.notes)

    piano.notes.sort(key=lambda n: (n.start, n.pitch, n.end))
    arranged.instruments.append(piano)
    output_midi.parent.mkdir(parents=True, exist_ok=True)
    arranged.write(str(output_midi))
