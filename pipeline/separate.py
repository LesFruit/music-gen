from __future__ import annotations

from pathlib import Path


def separate_stems(input_audio: Path, stems_dir: Path) -> dict[str, Path]:
    """MVP placeholder: return a pass-through stem map.

    Epic 2 integrates Demucs/Open-Unmix; this preserves a stable interface now.
    """

    stems_dir.mkdir(parents=True, exist_ok=True)
    passthrough = stems_dir / "other.wav"
    passthrough.write_bytes(input_audio.read_bytes())
    return {"other": passthrough}
