from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from coverctl.ace_step_jobs import add_ace_step_subcommands
from coverctl.suno_jobs import add_suno_subcommands
from pipeline.arrange_orchestra import arrange_orchestra
from pipeline.arrange_piano import arrange_piano
from pipeline.io import normalize_audio
from pipeline.manifest import JobManifest
from pipeline.metrics import compute_midi_metrics
from pipeline.midi_clean import clean_midi
from pipeline.render import render_midi_to_wav
from pipeline.transcribe import transcribe_audio


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coverctl", description="Audio cover pipeline CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run pipeline")
    run.add_argument("input", type=Path)
    run.add_argument("--style", choices=["piano", "orchestra"], required=True)
    run.add_argument("--output-dir", type=Path, default=Path("data/out"))
    run.add_argument("--job-id", type=str, default=None)
    run.add_argument("--transcriber", choices=["basicpitch", "mock"], default="basicpitch")
    run.add_argument("--soundfont", type=Path, default=None)
    run.set_defaults(func=run_command)

    add_suno_subcommands(sub)
    add_ace_step_subcommands(sub)

    anime = sub.add_parser(
        "anime-pipeline",
        help="Unified anime cover pipeline (download + ACE Step + Suno)",
    )
    anime.add_argument(
        "inputs", nargs="*", default=[],
        help="Audio files or directories to process",
    )
    anime.add_argument(
        "--from-list", default=None,
        help="Path to anime_songs.json — downloads sources first",
    )
    anime.add_argument("--output-dir", default="data/anime-covers")
    anime.add_argument(
        "--engine", action="append", choices=["ace-step", "suno"],
        help="Which engines to use (default: both)",
    )
    anime.add_argument("--duration", type=int, default=60)
    anime.add_argument("--model", default="chirp-crow", help="Suno model (chirp-crow = v5)")
    anime.add_argument("--timeout", type=float, default=300.0)
    anime.add_argument("--poll-interval", type=float, default=5.0)
    anime.add_argument("--pre-download-wait", type=float, default=20.0)
    anime.set_defaults(func=run_anime_pipeline)

    # anime-chain: Song -> ACE Step -> Suno chained pipeline
    chain = sub.add_parser(
        "anime-chain",
        help="Song -> ACE Step -> Suno chained cover pipeline",
    )
    chain.add_argument("source", type=Path, help="Source audio file (WAV/MP3)")
    chain.add_argument("--slug", required=True, help="Short name for the song (e.g. blue-bird)")
    chain.add_argument("--tags", default="anime, j-rock, cover", help="Style tags for ACE Step")
    chain.add_argument("--lyrics", default="", help="Romaji lyrics for ACE Step")
    chain.add_argument("--output-dir", type=Path, default=Path("data/anime-covers"))
    chain.add_argument("--duration", type=int, default=120, help="Cover duration in seconds")
    chain.add_argument(
        "--ace-variants", default="faithful,orchestral",
        help="Comma-separated ACE Step variants (faithful=0.2, orchestral=0.4, creative=0.6)",
    )
    chain.add_argument(
        "--suno-presets", default="rock,orchestral,city-pop,ballad",
        help="Comma-separated Suno preset names",
    )
    chain.add_argument("--model", default="chirp-crow", help="Suno model (chirp-crow = v5)")
    chain.add_argument("--timeout", type=float, default=300.0)
    chain.add_argument("--pre-download-wait", type=float, default=25.0)
    chain.set_defaults(func=run_anime_chain)

    # ace-batch: Batch ACE Step covers from song catalog
    ace_batch = sub.add_parser(
        "ace-batch",
        help="Batch ACE Step cover generation from song catalog",
    )
    ace_batch.add_argument(
        "--sources", default=None,
        help="Comma-separated slugs (default: all in catalog)",
    )
    ace_batch.add_argument(
        "--variants", default="faithful,orchestral",
        help="Comma-separated variants: faithful, orchestral, city-pop, lofi, edm, jazz-piano, epic-choir",
    )
    ace_batch.add_argument("--output-dir", type=Path, default=Path("data/anime-covers"))
    ace_batch.add_argument("--duration", type=int, default=120)
    ace_batch.set_defaults(func=run_ace_batch)

    return parser


def run_command(args: argparse.Namespace) -> int:
    input_audio = args.input.resolve()
    if not input_audio.exists():
        raise FileNotFoundError(f"Input audio not found: {input_audio}")

    job_id = args.job_id or str(uuid.uuid4())
    out_root = args.output_dir.resolve() / job_id
    out_root.mkdir(parents=True, exist_ok=True)

    normalized_audio = out_root / "normalized.wav"
    duration_s, sample_rate = normalize_audio(input_audio, normalized_audio)

    manifest = JobManifest.create(
        input_path=input_audio,
        duration_s=duration_s,
        sr=sample_rate,
        job_id=job_id,
    )
    manifest.add_artifact("normalized_audio", normalized_audio)

    transcription_midi = out_root / "transcription.mid"
    transcribe_audio(normalized_audio, transcription_midi, backend=args.transcriber)
    manifest.add_artifact("transcription_mid", transcription_midi)
    manifest.add_decision(f"transcriber={args.transcriber}")

    cleaned_midi = out_root / "transcription_clean.mid"
    clean_midi(transcription_midi, cleaned_midi)
    manifest.add_artifact("transcription_clean_mid", cleaned_midi)

    metrics = compute_midi_metrics(cleaned_midi)
    for key, value in metrics.items():
        manifest.add_metric(key, value)

    if args.style == "piano":
        cover_mid = out_root / "cover_piano.mid"
        cover_wav = out_root / "cover_piano.wav"
        arrange_piano(cleaned_midi, cover_mid)
        renderer = render_midi_to_wav(cover_mid, cover_wav, soundfont_path=args.soundfont)
        manifest.add_artifact("cover_piano_mid", cover_mid)
        manifest.add_artifact("cover_piano_wav", cover_wav)
        manifest.add_decision(f"renderer={renderer}")
    else:
        cover_mid = out_root / "cover_orchestra.mid"
        cover_wav = out_root / "cover_orchestra.wav"
        arrange_orchestra(cleaned_midi, cover_mid)
        renderer = render_midi_to_wav(cover_mid, cover_wav, soundfont_path=args.soundfont)
        manifest.add_artifact("cover_orchestra_mid", cover_mid)
        manifest.add_artifact("cover_orchestra_wav", cover_wav)
        manifest.add_decision(f"renderer={renderer}")

    manifest_path = out_root / "manifest.json"
    manifest.write(manifest_path)

    report_path = out_root / "report.json"
    report_payload = {
        "job_id": manifest.job_id,
        "style": args.style,
        "metrics": manifest.metrics,
        "decisions": manifest.decisions,
    }
    report_path.write_text(json.dumps(report_payload, indent=2), encoding="utf-8")
    manifest.add_artifact("report", report_path)
    manifest.write(manifest_path)

    print(
        json.dumps({"job_id": job_id, "output_dir": str(out_root), "manifest": str(manifest_path)})
    )
    return 0


def run_anime_pipeline(args: argparse.Namespace) -> int:
    from scripts.anime_cover_pipeline import run_pipeline

    return run_pipeline(args)


def run_anime_chain(args: argparse.Namespace) -> int:
    from scripts.anime_chain import run_chain

    return run_chain(args)


def run_ace_batch(args: argparse.Namespace) -> int:
    from scripts.ace_step_batch_covers import run_batch, SONG_CATALOG, VARIANTS

    slugs = (
        [s.strip() for s in args.sources.split(",")]
        if args.sources
        else sorted(SONG_CATALOG.keys())
    )
    variants = [v.strip() for v in args.variants.split(",")]
    manifest = run_batch(
        slugs=slugs,
        variants=variants,
        output_dir=args.output_dir.resolve(),
        duration=args.duration,
    )
    return 0 if manifest["summary"]["failed"] == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
