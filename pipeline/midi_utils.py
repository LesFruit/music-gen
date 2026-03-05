from __future__ import annotations

import math

import pretty_midi


def safe_estimate_tempo(midi: pretty_midi.PrettyMIDI, default: float = 120.0) -> float:
    try:
        tempo = float(midi.estimate_tempo())
    except (TypeError, ValueError, ZeroDivisionError):
        return default
    if not math.isfinite(tempo) or tempo <= 0:
        return default
    return tempo
