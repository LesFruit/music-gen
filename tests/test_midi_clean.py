from pathlib import Path

import pretty_midi

from pipeline.midi_clean import MidiCleanConfig, clean_midi


def _write_test_midi(path: Path) -> None:
    midi = pretty_midi.PrettyMIDI(initial_tempo=120)
    inst = pretty_midi.Instrument(program=0)
    inst.notes.append(pretty_midi.Note(velocity=90, pitch=10, start=0.0, end=0.01))  # too short
    inst.notes.append(pretty_midi.Note(velocity=90, pitch=110, start=0.1, end=0.5))  # out of range
    inst.notes.append(pretty_midi.Note(velocity=70, pitch=64, start=0.5, end=1.0))
    inst.notes.append(pretty_midi.Note(velocity=100, pitch=64, start=0.7, end=1.2))  # overlap
    midi.instruments.append(inst)
    midi.write(str(path))


def test_clean_midi_applies_v1_rules(tmp_path: Path) -> None:
    input_mid = tmp_path / "input.mid"
    output_mid = tmp_path / "output.mid"
    _write_test_midi(input_mid)

    clean_midi(
        input_mid, output_mid, config=MidiCleanConfig(min_note_ms=60, quantize_subdivision=4)
    )

    cleaned = pretty_midi.PrettyMIDI(str(output_mid))
    notes = cleaned.instruments[0].notes

    assert notes
    assert all((n.end - n.start) >= 0.06 for n in notes)
    assert all(21 <= n.pitch <= 108 for n in notes)

    pitch64 = [n for n in notes if n.pitch == 64]
    assert len(pitch64) == 1


def test_clean_midi_handles_single_note_input(tmp_path: Path) -> None:
    input_mid = tmp_path / "single.mid"
    output_mid = tmp_path / "single-clean.mid"

    midi = pretty_midi.PrettyMIDI(initial_tempo=120)
    inst = pretty_midi.Instrument(program=0)
    inst.notes.append(pretty_midi.Note(velocity=100, pitch=60, start=0.0, end=0.3))
    midi.instruments.append(inst)
    midi.write(str(input_mid))

    clean_midi(input_mid, output_mid)
    cleaned = pretty_midi.PrettyMIDI(str(output_mid))
    assert len(cleaned.instruments) == 1
    assert len(cleaned.instruments[0].notes) == 1
