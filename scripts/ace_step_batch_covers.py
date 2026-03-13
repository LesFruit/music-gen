#!/usr/bin/env python3
"""Batch ACE Step cover generation via SSH to gpu-dev-3.

Generates faithful, orchestral, and creative variants of source audio files.
Each variant uses a different noise strength to achieve different levels of
divergence from the original.

Usage:
    # Cover all sources on gpu-dev-3
    coverctl ace-step batch-covers --output-dir data/anime-covers

    # Cover specific sources
    coverctl ace-step batch-covers --sources gurenge,kaikai-kitan

    # Standalone
    python scripts/ace_step_batch_covers.py --output-dir data/anime-covers
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# ── Configuration ────────────────────────────────────────────────────

ACE_STEP_HOST = "100.116.10.41"
ACE_STEP_DIR = "/srv/ace-step"
LD_LIB = "/usr/lib/wsl/lib:/usr/lib/wsl/drivers"
REMOTE_SRC_DIR = "/host/d/Music/suno/popular-sources"
REMOTE_OUTPUT_DIR = "/tmp/ace-step-anime/output"

# Variant definitions: name -> (noise_strength, tags_override or None)
VARIANTS: dict[str, dict[str, Any]] = {
    "faithful": {
        "noise": 0.2,
        "tags_override": None,
        "description": "Close to original melody (90-95% similarity)",
    },
    "orchestral": {
        "noise": 0.4,
        "tags_override": "orchestral, cinematic, strings, piano, brass, emotional, soundtrack, epic",
        "description": "Orchestral reimagining (70-80% similarity)",
    },
    "city-pop": {
        "noise": 0.5,
        "tags_override": "city pop, glossy synths, bass groove, nostalgic, bright, polished, 80s japanese pop",
        "description": "City pop / 80s J-pop style",
    },
    "lofi": {
        "noise": 0.5,
        "tags_override": "lo-fi, chill, vinyl crackle, relaxing, mellow, study beats, jazzy",
        "description": "Lo-fi chill / study beats",
    },
    "edm": {
        "noise": 0.5,
        "tags_override": "electronic, EDM, synth, upbeat, energetic, remix, powerful, bass drop",
        "description": "Electronic / EDM remix",
    },
    "jazz-piano": {
        "noise": 0.5,
        "tags_override": "jazz, piano, smooth, emotional, intimate, late night, sophisticated",
        "description": "Jazz piano arrangement",
    },
    "epic-choir": {
        "noise": 0.5,
        "tags_override": "epic, orchestral, choir, cinematic, powerful, dramatic, battle, triumph",
        "description": "Epic orchestral with choir",
    },
}

# Source catalog: slug -> source file on gpu-dev-3 + metadata
SONG_CATALOG: dict[str, dict[str, str]] = {
    "gurenge": {
        "file": "Demon-Slayer-Gurenge-LiSA.wav",
        "tags": "anime, j-rock, powerful, dramatic, emotional, LiSA",
        "lyrics": "kawaranai mono nante nai nda yo furikaeru bakka no michijya",
        "anime": "Demon Slayer",
        "artist": "LiSA",
    },
    "kaikai-kitan": {
        "file": "Jujutsu-Kaisen-Opening-Kaikai-Kitan.wav",
        "tags": "anime, j-rock, fast, rhythmic, dark, mysterious, Eve",
        "lyrics": "itsuka kieta hikari ima mo sagashiteru",
        "anime": "Jujutsu Kaisen",
        "artist": "Eve",
    },
    "shinzou-wo-sasageyo": {
        "file": "Attack-on-Titan-Shinzou-wo-Sasageyo.wav",
        "tags": "anime, epic, powerful, dramatic, choir, marching",
        "lyrics": "sasageyo sasageyo shinzou wo sasageyo",
        "anime": "Attack on Titan S2",
        "artist": "Linked Horizon",
    },
    "haruka-kanata": {
        "file": "ASIAN KUNG-FU GENERATION - Haruka Kanata.wav",
        "tags": "anime, j-rock, energetic, upbeat, nostalgic, guitar",
        "lyrics": "fumikomu ze akuseru kake hiki wa nai sa",
        "anime": "Naruto",
        "artist": "Asian Kung-Fu Generation",
    },
    "go-fighting-dreamers": {
        "file": "FLOW - Go!!! (Music Video).wav",
        "tags": "anime, j-rock, energetic, upbeat, rock, shounen",
        "lyrics": "we are fighting dreamers takami wo mezashite",
        "anime": "Naruto",
        "artist": "FLOW",
    },
    "sign-flow": {
        "file": "Naruto Shippuden Opening 6 ｜ Sign by FLOW.wav",
        "tags": "anime, j-rock, emotional, dramatic, powerful",
        "lyrics": "i realize the screaming pain hearing loud in my brain",
        "anime": "Naruto Shippuden",
        "artist": "FLOW",
    },
    "blue-bird": {
        "file": "Naruto-Blue-Bird-Opening.wav",
        "tags": "anime, j-pop, hopeful, bright, soaring vocal",
        "lyrics": "habataitara modoranai to itte mezashita no wa aoi aoi ano sora",
        "anime": "Naruto Shippuden",
        "artist": "Ikimonogakari",
    },
    "silhouette": {
        "file": "KANA-BOON - Silhouette.wav",
        "tags": "anime, j-rock, energetic, catchy, powerful, anthem",
        "lyrics": "itsuka wa mita yume ima demo mite iru kara hashire hashiridase",
        "anime": "Naruto Shippuden",
        "artist": "KANA-BOON",
    },
    "we-are-one-piece": {
        "file": "One-Piece-We-Are-Opening.wav",
        "tags": "anime, j-pop, adventure, upbeat, bright, shounen",
        "lyrics": "arittake no yume wo kaki atsume sagashi mono sagashi ni yuku no sa one piece",
        "anime": "One Piece",
        "artist": "Hiroshi Kitadani",
    },
    "blinding-lights": {
        "file": "The-Weeknd-Blinding-Lights-official-audio.wav",
        "tags": "synthwave, 80s, pop, retro, nostalgic, driving",
        "lyrics": "i been tryna call i been on my own for long enough",
        "anime": "",
        "artist": "The Weeknd",
    },
    "shape-of-you": {
        "file": "Ed-Sheeran-Shape-of-You-official-audio.wav",
        "tags": "pop, tropical house, groovy, catchy, romantic",
        "lyrics": "im in love with the shape of you",
        "anime": "",
        "artist": "Ed Sheeran",
    },
    "drivers-license": {
        "file": "Olivia-Rodrigo-drivers-license-official-audio.wav",
        "tags": "pop, emotional, piano, heartbreak, ballad",
        "lyrics": "i got my drivers license last week just like we always talked about",
        "anime": "",
        "artist": "Olivia Rodrigo",
    },
    "believer": {
        "file": "Imagine-Dragons-Believer-official-audio.wav",
        "tags": "rock, anthemic, powerful, drums, motivational",
        "lyrics": "first things first i ma say all the words inside my head",
        "anime": "",
        "artist": "Imagine Dragons",
    },
    "idol-yoasobi": {
        "file": "YOASOBI-Idol-official-audio.wav",
        "tags": "anime, j-pop, energetic, catchy, bright, powerful, idol, fast",
        "lyrics": "tokubetsu janai kimi janai shinjitsu no uta utau",
        "anime": "Oshi no Ko",
        "artist": "YOASOBI",
    },
    # Demon Slayer extended catalog
    "zankyou-sanka": {
        "file": "zankyou-sanka.wav",
        "tags": "anime, j-pop, emotional, powerful, soaring vocal, dramatic, Aimer",
        "lyrics": "todokanai todokanainda kono koe wa",
        "anime": "Demon Slayer S2",
        "artist": "Aimer",
    },
    "homura": {
        "file": "homura-lisa.wav",
        "tags": "anime, j-pop, emotional, ballad, powerful, dramatic, cinematic, LiSA",
        "lyrics": "sayonara arigato koe no kagiri sakende",
        "anime": "Demon Slayer: Mugen Train",
        "artist": "LiSA",
    },
    "akeboshi": {
        "file": "akeboshi.wav",
        "tags": "anime, j-rock, energetic, upbeat, punk rock, fast, powerful, 10-FEET",
        "lyrics": "hikari wo motomete hashiru akeboshi",
        "anime": "Demon Slayer S3",
        "artist": "10-FEET",
    },
    "from-the-edge": {
        "file": "from-the-edge.wav",
        "tags": "anime, j-pop, emotional, dramatic, strings, powerful, FictionJunction",
        "lyrics": "kono te nobashite hikari motometa",
        "anime": "Demon Slayer S1 ED",
        "artist": "FictionJunction feat. LiSA",
    },
    "kizuna-no-kiseki": {
        "file": "kizuna-no-kiseki.wav",
        "tags": "anime, j-rock, powerful, emotional, dramatic, epic, duo vocal",
        "lyrics": "tsuyoku nareru riyuu wo shitta boku wo tsurete susume",
        "anime": "Demon Slayer S3 ED",
        "artist": "MAN WITH A MISSION x milet",
    },
    "shirogane": {
        "file": "shirogane.wav",
        "tags": "anime, j-rock, intense, powerful, emotional, dramatic, MY FIRST STORY",
        "lyrics": "shirogane no yoru ni kirameku",
        "anime": "Demon Slayer S4",
        "artist": "MY FIRST STORY",
    },
    # Game BGMs (instrumental — pass Suno upload at full length)
    "hollow-knight-city-of-tears": {
        "file": "Hollow-Knight-City-of-Tears-BGM.wav",
        "tags": "ambient, melancholy, piano, strings, rain, atmospheric, game soundtrack",
        "lyrics": "",
        "anime": "Hollow Knight",
        "artist": "Christopher Larkin",
        "suno_full_length": True,  # Not in Suno's fingerprint DB
    },
    "minecraft-sweden": {
        "file": "Minecraft-Sweden-C418.wav",
        "tags": "ambient, piano, peaceful, nostalgic, minimalist, game soundtrack",
        "lyrics": "",
        "anime": "Minecraft",
        "artist": "C418",
        "suno_full_length": True,
    },
    "stardew-valley-spring": {
        "file": "Stardew-Valley-Spring-Theme.wav",
        "tags": "folk, acoustic, cheerful, pastoral, country, game soundtrack",
        "lyrics": "",
        "anime": "Stardew Valley",
        "artist": "ConcernedApe",
        "suno_full_length": True,
    },
    "animal-crossing-main": {
        "file": "Animal-Crossing-Main-Theme.wav",
        "tags": "cheerful, whimsical, acoustic guitar, playful, cozy, game soundtrack",
        "lyrics": "",
        "anime": "Animal Crossing",
        "artist": "Kazumi Totaka",
        "suno_full_length": True,
    },
    "undertale-megalovania": {
        "file": "Undertale-Megalovania-Toby-Fox.wav",
        "tags": "chiptune, rock, intense, energetic, boss battle, game soundtrack",
        "lyrics": "",
        "anime": "Undertale",
        "artist": "Toby Fox",
    },
}


def run_ace_step_cover(
    source_file: str,
    tags: str,
    lyrics: str,
    noise: float,
    duration: int,
    title: str,
) -> str:
    """Run ACE Step cover on gpu-dev-3 via SSH, return remote output path."""
    cmd = (
        f"cd {ACE_STEP_DIR} && "
        f"LD_LIBRARY_PATH={LD_LIB} "
        f".venv/bin/python generate_cli.py "
        f"--tags '{tags}' "
        f"--lyrics '{lyrics}' "
        f"--ref-audio '{REMOTE_SRC_DIR}/{source_file}' "
        f"--cover-noise-strength {noise} "
        f"--audio-cover-strength 1.0 "
        f"--duration {duration} "
        f"--title '{title}' "
        f"--output-dir '{REMOTE_OUTPUT_DIR}'"
    )
    result = subprocess.run(
        ["ssh", ACE_STEP_HOST, cmd],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ACE Step failed: {result.stderr[-500:]}")

    for line in result.stdout.splitlines():
        if line.startswith("OUTPUT:"):
            return line.split("OUTPUT:", 1)[1].strip()

    raise RuntimeError(f"No OUTPUT line in ACE Step output:\n{result.stdout[-500:]}")


def download_from_remote(remote_path: str, local_path: Path) -> None:
    """SCP file from gpu-dev-3 to local."""
    local_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["scp", f"{ACE_STEP_HOST}:{remote_path}", str(local_path)],
        check=True, capture_output=True, timeout=120,
    )


def run_batch(
    slugs: list[str],
    variants: list[str],
    output_dir: Path,
    duration: int = 120,
) -> dict[str, Any]:
    """Generate ACE Step covers for all slugs x variants.

    Returns a manifest dict with results.
    """
    results: dict[str, dict[str, Any]] = {}
    total = len(slugs) * len(variants)
    completed = 0
    skipped = 0
    failed = 0

    print(f"[ace-batch] Songs: {len(slugs)} | Variants: {len(variants)} | Total: {total}")
    print()

    for slug in slugs:
        song = SONG_CATALOG.get(slug)
        if not song:
            print(f"[ace-batch] Unknown slug '{slug}', skipping")
            continue

        results[slug] = {}

        for variant_name in variants:
            variant = VARIANTS.get(variant_name)
            if not variant:
                print(f"[ace-batch] Unknown variant '{variant_name}', skipping")
                continue

            out_subdir = output_dir / slug / "ace-step" / variant_name
            done_marker = out_subdir / ".done"

            if done_marker.exists():
                existing = list(out_subdir.glob("*.wav"))
                if existing:
                    results[slug][variant_name] = {
                        "status": "skipped",
                        "path": str(existing[0]),
                    }
                    skipped += 1
                    print(f"  [{slug}/{variant_name}] SKIP (already done)")
                    continue

            # Build tags
            tags = variant["tags_override"] or song["tags"]
            title = f"{slug}-{variant_name}"
            noise = variant["noise"]

            print(f"  [{slug}/{variant_name}] Generating (noise={noise})...")

            try:
                remote_path = run_ace_step_cover(
                    source_file=song["file"],
                    tags=tags,
                    lyrics=song["lyrics"],
                    noise=noise,
                    duration=duration,
                    title=title,
                )

                local_path = out_subdir / Path(remote_path).name
                download_from_remote(remote_path, local_path)
                done_marker.parent.mkdir(parents=True, exist_ok=True)
                done_marker.write_text("ok\n")

                results[slug][variant_name] = {
                    "status": "complete",
                    "path": str(local_path),
                }
                completed += 1
                print(f"  [{slug}/{variant_name}] ✓ {local_path.name}")

            except Exception as exc:
                results[slug][variant_name] = {
                    "status": "error",
                    "error": str(exc),
                }
                failed += 1
                print(f"  [{slug}/{variant_name}] FAILED: {exc}")

    # Summary
    print()
    print(f"{'='*60}")
    print(f"[ace-batch] Complete: {completed} done, {skipped} skipped, {failed} failed")
    print(f"{'='*60}")

    return {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "songs": results,
        "summary": {
            "completed": completed,
            "skipped": skipped,
            "failed": failed,
            "total": total,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Batch ACE Step cover generation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Variants:
  faithful     Close to original (noise=0.2)
  orchestral   Orchestral reimagining (noise=0.4)
  city-pop     City pop / 80s style (noise=0.5)
  lofi         Lo-fi chill beats (noise=0.5)
  edm          EDM remix (noise=0.5)
  jazz-piano   Jazz piano arrangement (noise=0.5)
  epic-choir   Epic orchestral + choir (noise=0.5)

Available songs: """ + ", ".join(sorted(SONG_CATALOG.keys())),
    )
    parser.add_argument(
        "--sources", default=None,
        help="Comma-separated slugs (default: all available)",
    )
    parser.add_argument(
        "--variants", default="faithful,orchestral",
        help="Comma-separated variant names (default: faithful,orchestral)",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("data/anime-covers"),
    )
    parser.add_argument("--duration", type=int, default=120)

    args = parser.parse_args(argv)

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

    # Write manifest
    manifest_path = args.output_dir / "batch_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nManifest: {manifest_path}")

    return 0 if manifest["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
