from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

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


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
