from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

SUPPORTED_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".mp4"}
# Suno v5 model. "chirp-crow" = v5 (released Sep 2025).
# Always use v5 — older models (chirp-chirp=v4.5, chirp-v3=v3.5) are deprecated.
DEFAULT_MODEL = "chirp-crow"
DEFAULT_BATCH_STYLES = {
    "anime-rock": "anime, j-rock, powerful, electric guitar, live drums, emotional, cinematic",
    "anime-orchestral": "anime, orchestral, cinematic, strings, piano, emotional, soundtrack",
    "anime-city-pop": "anime, city pop, glossy synths, bass groove, nostalgic, bright, polished",
    "anime-ballad": "anime, emotional ballad, piano, strings, soaring vocal, heartfelt",
}


def add_suno_subcommands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    suno = subparsers.add_parser("suno", help="Run Suno generation and cover workflows")
    suno_sub = suno.add_subparsers(dest="suno_command", required=True)

    generate = suno_sub.add_parser("generate", help="Generate a song from a text prompt")
    generate.add_argument("prompt")
    generate.add_argument("--output-dir", type=Path, default=Path("data/suno"))
    generate.add_argument("--job-id", type=str, default=None)
    generate.add_argument("--title", default="")
    generate.add_argument("--tags", default="")
    generate.add_argument("--custom", action="store_true", help="Treat prompt as custom lyrics")
    generate.add_argument("--instrumental", action="store_true")
    generate.add_argument("--model", default=DEFAULT_MODEL)
    generate.add_argument("--timeout", type=float, default=300.0)
    generate.add_argument("--poll-interval", type=float, default=5.0)
    generate.add_argument(
        "--wav",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Convert downloaded clips to WAV",
    )
    generate.set_defaults(func=run_suno_generate)

    cover = suno_sub.add_parser("cover", help="Upload one audio file and create Suno cover outputs")
    cover.add_argument("input", type=Path)
    cover.add_argument("--output-dir", type=Path, default=Path("data/suno-covers"))
    cover.add_argument("--job-id", type=str, default=None)
    cover.add_argument("--prompt", default="")
    cover.add_argument("--title", default="")
    cover.add_argument("--tags", default="")
    cover.add_argument("--instrumental", action="store_true")
    cover.add_argument("--model", default=DEFAULT_MODEL)
    cover.add_argument("--timeout", type=float, default=300.0)
    cover.add_argument("--poll-interval", type=float, default=5.0)
    cover.add_argument("--pre-download-wait", type=float, default=20.0)
    cover.add_argument(
        "--wav",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Convert downloaded clips to WAV",
    )
    cover.set_defaults(func=run_suno_cover)

    batch = suno_sub.add_parser("cover-batch", help="Create Suno covers for every file in a folder")
    batch.add_argument("input_dir", type=Path)
    batch.add_argument("--output-dir", type=Path, default=Path("data/suno-batches"))
    batch.add_argument("--tags", default="")
    batch.add_argument("--prompt", default="")
    batch.add_argument("--title-template", default="{stem_clean} cover")
    batch.add_argument("--instrumental", action="store_true")
    batch.add_argument("--model", default=DEFAULT_MODEL)
    batch.add_argument("--timeout", type=float, default=300.0)
    batch.add_argument("--poll-interval", type=float, default=5.0)
    batch.add_argument("--pre-download-wait", type=float, default=20.0)
    batch.add_argument("--sleep-between", type=float, default=0.0)
    batch.add_argument("--recursive", action="store_true")
    batch.add_argument("--resume", action="store_true")
    batch.add_argument(
        "--wav",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Convert downloaded clips to WAV",
    )
    batch.set_defaults(func=run_suno_cover_batch)

    anime = suno_sub.add_parser(
        "anime-batch",
        help="Run a batch cover pipeline over downloaded anime tracks",
    )
    anime.add_argument("input_dir", type=Path)
    anime.add_argument("--output-dir", type=Path, default=Path("data/anime-covers"))
    anime.add_argument("--preset", choices=sorted(DEFAULT_BATCH_STYLES), default="anime-rock")
    anime.add_argument("--prompt", default="")
    anime.add_argument("--title-template", default="{stem_clean} {preset_label}")
    anime.add_argument("--instrumental", action="store_true")
    anime.add_argument("--model", default=DEFAULT_MODEL)
    anime.add_argument("--timeout", type=float, default=300.0)
    anime.add_argument("--poll-interval", type=float, default=5.0)
    anime.add_argument("--pre-download-wait", type=float, default=20.0)
    anime.add_argument("--sleep-between", type=float, default=0.0)
    anime.add_argument("--recursive", action="store_true")
    anime.add_argument("--resume", action="store_true")
    anime.add_argument(
        "--wav",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Convert downloaded clips to WAV",
    )
    anime.set_defaults(func=run_suno_anime_batch)


def _slugify(text: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "-", text.strip())
    value = value.strip("-")
    return value or "untitled"


def _iter_audio_files(folder: Path, recursive: bool) -> list[Path]:
    search = folder.rglob("*") if recursive else folder.glob("*")
    return sorted(
        path for path in search if path.is_file() and path.suffix.lower() in SUPPORTED_EXTS
    )


def _job_dir(base_dir: Path, job_id: str) -> Path:
    job_dir = base_dir.resolve() / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir


def _resolve_job_id(value: str | None, default_stem: str) -> str:
    return value or f"{_slugify(default_stem)}-{uuid.uuid4().hex[:8]}"


def _write_manifest(output_dir: Path, payload: dict[str, Any]) -> Path:
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return manifest_path


def _title_from_template(template: str, stem: str, preset_label: str = "") -> str:
    stem_clean = stem.replace("-", " ").replace("_", " ").strip()
    return template.format(stem=stem, stem_clean=stem_clean, preset_label=preset_label).strip()


def _get_suno_value(key: str) -> str:
    from suno_wrapper.env_util import env_fallback

    return os.environ.get(key, "").strip() or env_fallback(key)


def _build_client(model: str) -> Any:
    from suno_wrapper import SunoClient
    from suno_wrapper.env_util import reload_env_to_os

    reload_env_to_os()
    return SunoClient(
        cookie=_get_suno_value("SUNO_COOKIE"),
        auth_token=_get_suno_value("SUNO_AUTH_TOKEN"),
        device_id=_get_suno_value("SUNO_DEVICE_ID"),
        browser_token=_get_suno_value("SUNO_BROWSER_TOKEN"),
        api_session_id=_get_suno_value("SUNO_API_SESSION_ID"),
        model_version=model,
    )


def _required_web_env() -> tuple[str, str, str | None]:
    generate_token = _get_suno_value("SUNO_GENERATE_TOKEN")
    project_id = _get_suno_value("SUNO_PROJECT_ID")
    transaction_uuid = _get_suno_value("SUNO_TRANSACTION_UUID") or None
    if not generate_token or not project_id:
        raise RuntimeError("Missing SUNO_GENERATE_TOKEN or SUNO_PROJECT_ID for Suno web cover flow")
    return generate_token, project_id, transaction_uuid


async def _download_clips(
    client: Any,
    clips: list[Any],
    output_dir: Path,
    stem: str,
    convert_to_wav: bool,
) -> list[str]:
    downloads: list[str] = []
    for index, clip in enumerate(clips, start=1):
        if not clip.audio_url:
            continue
        filename = f"{_slugify(stem)}_clip{index}"
        path = await client.download_audio(
            clip=clip,
            output_dir=str(output_dir),
            filename=filename,
            convert_to_wav=convert_to_wav,
        )
        downloads.append(str(path))
    return downloads


async def _run_generate(args: argparse.Namespace) -> int:
    job_id = _resolve_job_id(args.job_id, args.title or args.prompt[:48])
    output_dir = _job_dir(args.output_dir, job_id)

    client = _build_client(args.model)
    try:
        clips = await client.generate(
            prompt=args.prompt,
            is_custom=args.custom,
            tags=args.tags,
            title=args.title,
            make_instrumental=args.instrumental,
            model_version=args.model,
            wait_for_completion=True,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            token=_get_suno_value("SUNO_GENERATE_TOKEN"),
        )
        downloads = await _download_clips(
            client,
            clips,
            output_dir,
            args.title or "generated-song",
            args.wav,
        )
    finally:
        await client.close()

    manifest_path = _write_manifest(
        output_dir,
        {
            "mode": "suno-generate",
            "job_id": job_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "prompt": args.prompt,
            "tags": args.tags,
            "title": args.title,
            "instrumental": args.instrumental,
            "downloads": downloads,
            "clips": [{"id": clip.id, "title": clip.title, "status": clip.status} for clip in clips],
        },
    )
    print(json.dumps({"job_id": job_id, "output_dir": str(output_dir), "manifest": str(manifest_path)}))
    return 0 if downloads else 5


async def _solve_captcha_and_refresh() -> bool:
    """Attempt to solve captcha and refresh Suno tokens.

    Returns True if a new token was obtained and saved.
    """
    try:
        from suno_wrapper.captcha_solver import CaptchaSolver
        from suno_wrapper.env_util import reload_env_to_os, save_token

        solver = CaptchaSolver()
        result = await solver.solve()
        if result.success and result.token:
            save_token(result.token)
            reload_env_to_os()
            print(f"[suno] Captcha solved via {result.method}, token refreshed")
            return True
        print(f"[suno] Captcha solve failed: {result.error}")
    except Exception as exc:
        print(f"[suno] Captcha solver error: {exc}")
    return False


_CAPTCHA_AUTH_KEYWORDS = ("captcha", "token validation", "unauthorized", "403", "401")

# Suno content fingerprinting errors that indicate the uploaded audio was
# detected as copyrighted material.
_FINGERPRINT_KEYWORDS = ("matches existing work of art", "copyrighted lyrics")

# Maximum clip duration (in seconds) that bypasses Suno's content fingerprint.
# Empirically determined: 15s passes, 20s gets caught.
FINGERPRINT_BYPASS_DURATION = 15


def _is_captcha_or_auth_error(exc: Exception) -> bool:
    """Check if an exception looks like a captcha or auth failure."""
    msg = str(exc).lower()
    return any(kw in msg for kw in _CAPTCHA_AUTH_KEYWORDS)


def _is_fingerprint_error(exc: Exception) -> bool:
    """Check if an exception is Suno's content fingerprint rejection."""
    msg = str(exc).lower()
    return any(kw in msg for kw in _FINGERPRINT_KEYWORDS)


def _trim_audio_for_upload(input_path: Path, duration: int = FINGERPRINT_BYPASS_DURATION) -> Path:
    """Trim audio to a short clip to bypass Suno's content fingerprinting.

    Returns path to a temporary trimmed MP3 file.
    Suno's content ID catches clips >= 20s but passes clips <= 15s.
    """
    import subprocess
    import tempfile

    out = Path(tempfile.mktemp(suffix=".mp3"))
    subprocess.run(
        [
            "ffmpeg", "-i", str(input_path),
            "-ss", "5",           # skip first 5s (intros are recognizable)
            "-t", str(duration),
            "-ar", "44100", "-ac", "2",
            "-codec:a", "libmp3lame", "-b:a", "192k",
            str(out), "-y",
        ],
        capture_output=True, timeout=30,
    )
    return out


async def _run_cover_job(
    *,
    input_path: Path,
    output_dir: Path,
    prompt: str,
    title: str,
    tags: str,
    instrumental: bool,
    model: str,
    timeout: float,
    poll_interval: float,
    pre_download_wait: float,
    wav: bool,
    _max_captcha_retries: int = 2,
    trim_for_fingerprint: bool = False,
) -> dict[str, Any]:
    last_exc: Exception | None = None
    upload_path = input_path

    # If trim requested, create a short clip to bypass content fingerprinting
    if trim_for_fingerprint:
        upload_path = _trim_audio_for_upload(input_path)
        print(f"[suno] Trimmed to {FINGERPRINT_BYPASS_DURATION}s for fingerprint bypass")

    for attempt in range(_max_captcha_retries + 1):
        generate_token, project_id, transaction_uuid = _required_web_env()
        client = _build_client(model)
        try:
            upload = await client.upload_audio_file(upload_path, initialize=True)
            cover_clip_id = str(upload.get("clip_id", "")).strip()
            if not cover_clip_id:
                raise RuntimeError(f"Upload completed but no clip_id was returned for {input_path}")

            clips = await client.generate_v2_web(
                prompt=prompt,
                generate_token=generate_token,
                project_id=project_id,
                transaction_uuid=transaction_uuid,
                task="cover",
                is_custom=True,
                tags=tags,
                title=title or input_path.stem,
                make_instrumental=instrumental,
                model_version=model,
                wait_for_completion=True,
                timeout=timeout,
                poll_interval=poll_interval,
                generation_type="TEXT",
                cover_clip_id=cover_clip_id,
            )
            if pre_download_wait > 0:
                await asyncio.sleep(pre_download_wait)
            downloads = await _download_clips(client, clips, output_dir, input_path.stem, wav)
            return {
                "input": str(input_path),
                "upload": upload,
                "downloads": downloads,
                "clips": [{"id": clip.id, "title": clip.title, "status": clip.status} for clip in clips],
            }
        except Exception as exc:
            last_exc = exc
            if _is_fingerprint_error(exc) and not trim_for_fingerprint:
                # Retry with trimmed audio to bypass content fingerprinting
                print(f"[suno] Content fingerprint detected, retrying with {FINGERPRINT_BYPASS_DURATION}s trim...")
                await client.close()
                upload_path = _trim_audio_for_upload(input_path)
                trim_for_fingerprint = True
                continue
            if _is_captcha_or_auth_error(exc) and attempt < _max_captcha_retries:
                print(f"[suno] Auth/captcha error (attempt {attempt + 1}): {exc}")
                await client.close()
                solved = await _solve_captcha_and_refresh()
                if not solved:
                    print("[suno] Could not resolve captcha, will retry with current tokens")
                continue
            raise
        finally:
            await client.close()
            # Clean up temp file if we created one
            if upload_path != input_path and upload_path.exists():
                upload_path.unlink(missing_ok=True)

    # Should not reach here, but just in case
    raise last_exc or RuntimeError("Cover job failed after retries")


async def _run_single_cover(args: argparse.Namespace) -> int:
    input_path = args.input.resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input audio not found: {input_path}")
    job_id = _resolve_job_id(args.job_id, input_path.stem)
    output_dir = _job_dir(args.output_dir, job_id)
    result = await _run_cover_job(
        input_path=input_path,
        output_dir=output_dir,
        prompt=args.prompt,
        title=args.title or f"{input_path.stem} cover",
        tags=args.tags,
        instrumental=args.instrumental,
        model=args.model,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
        pre_download_wait=args.pre_download_wait,
        wav=args.wav,
    )
    manifest_path = _write_manifest(
        output_dir,
        {
            "mode": "suno-cover",
            "job_id": job_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **result,
        },
    )
    print(json.dumps({"job_id": job_id, "output_dir": str(output_dir), "manifest": str(manifest_path)}))
    return 0 if result["downloads"] else 5


async def _run_batch(
    *,
    input_dir: Path,
    output_dir: Path,
    tags: str,
    prompt: str,
    title_template: str,
    preset_label: str,
    instrumental: bool,
    model: str,
    timeout: float,
    poll_interval: float,
    pre_download_wait: float,
    wav: bool,
    recursive: bool,
    resume: bool,
    sleep_between: float,
) -> int:
    files = _iter_audio_files(input_dir.resolve(), recursive=recursive)
    if not files:
        raise FileNotFoundError(f"No supported audio files found in {input_dir}")

    output_root = output_dir.resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    success_count = 0
    for input_path in files:
        job_id = _slugify(input_path.stem)
        job_dir = output_root / job_id
        if resume and (job_dir / ".done").exists():
            records.append({"input": str(input_path), "status": "skipped"})
            continue

        job_dir.mkdir(parents=True, exist_ok=True)
        try:
            result = await _run_cover_job(
                input_path=input_path,
                output_dir=job_dir,
                prompt=prompt,
                title=_title_from_template(title_template, input_path.stem, preset_label),
                tags=tags,
                instrumental=instrumental,
                model=model,
                timeout=timeout,
                poll_interval=poll_interval,
                pre_download_wait=pre_download_wait,
                wav=wav,
            )
            (job_dir / ".done").write_text("ok\n", encoding="utf-8")
            records.append({"input": str(input_path), "status": "complete", **result})
            success_count += 1
        except Exception as exc:
            records.append({"input": str(input_path), "status": "error", "error": f"{type(exc).__name__}: {exc}"})

        if sleep_between > 0:
            await asyncio.sleep(sleep_between)

    manifest_path = _write_manifest(
        output_root,
        {
            "mode": "suno-cover-batch",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "input_dir": str(input_dir.resolve()),
            "output_dir": str(output_root),
            "preset_label": preset_label,
            "tags": tags,
            "records": records,
            "summary": {
                "total": len(files),
                "succeeded": success_count,
                "failed": len([record for record in records if record["status"] == "error"]),
                "skipped": len([record for record in records if record["status"] == "skipped"]),
            },
        },
    )
    print(json.dumps({"output_dir": str(output_root), "manifest": str(manifest_path), "succeeded": success_count}))
    return 0 if success_count else 4


def run_suno_generate(args: argparse.Namespace) -> int:
    return asyncio.run(_run_generate(args))


def run_suno_cover(args: argparse.Namespace) -> int:
    return asyncio.run(_run_single_cover(args))


def run_suno_cover_batch(args: argparse.Namespace) -> int:
    return asyncio.run(
        _run_batch(
            input_dir=args.input_dir,
            output_dir=args.output_dir,
            tags=args.tags,
            prompt=args.prompt,
            title_template=args.title_template,
            preset_label="batch cover",
            instrumental=args.instrumental,
            model=args.model,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            pre_download_wait=args.pre_download_wait,
            wav=args.wav,
            recursive=args.recursive,
            resume=args.resume,
            sleep_between=args.sleep_between,
        )
    )


def run_suno_anime_batch(args: argparse.Namespace) -> int:
    preset = args.preset
    return asyncio.run(
        _run_batch(
            input_dir=args.input_dir,
            output_dir=args.output_dir,
            tags=DEFAULT_BATCH_STYLES[preset],
            prompt=args.prompt,
            title_template=args.title_template,
            preset_label=preset.replace("-", " "),
            instrumental=args.instrumental,
            model=args.model,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            pre_download_wait=args.pre_download_wait,
            wav=args.wav,
            recursive=args.recursive,
            resume=args.resume,
            sleep_between=args.sleep_between,
        )
    )
