from __future__ import annotations

from pathlib import Path

from coverctl.__main__ import _build_parser
from coverctl.suno_jobs import DEFAULT_BATCH_STYLES, _iter_audio_files


def test_parser_supports_suno_anime_batch() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        [
            "suno",
            "anime-batch",
            "data/in",
            "--preset",
            "anime-orchestral",
            "--recursive",
        ]
    )

    assert args.command == "suno"
    assert args.suno_command == "anime-batch"
    assert args.preset == "anime-orchestral"
    assert args.recursive is True


def test_iter_audio_files_filters_supported_extensions(tmp_path: Path) -> None:
    (tmp_path / "song.mp3").write_text("x", encoding="utf-8")
    (tmp_path / "clip.wav").write_text("x", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")

    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "theme.flac").write_text("x", encoding="utf-8")

    flat_files = _iter_audio_files(tmp_path, recursive=False)
    recursive_files = _iter_audio_files(tmp_path, recursive=True)

    assert [path.name for path in flat_files] == ["clip.wav", "song.mp3"]
    assert sorted(path.name for path in recursive_files) == ["clip.wav", "song.mp3", "theme.flac"]


def test_anime_presets_include_expected_styles() -> None:
    assert "anime-rock" in DEFAULT_BATCH_STYLES
    assert "anime-orchestral" in DEFAULT_BATCH_STYLES
    assert "cinematic" in DEFAULT_BATCH_STYLES["anime-orchestral"]
