# Music Cover Generation Guide

## Overview

This document covers all tested approaches for generating music covers using the music-gen project infrastructure. Tested with "Blue Bird" (Naruto opening by Ikimonogakari) on 2026-03-10.

## Available Models & Approaches

### 1. ACE-Step 1.5 (BEST for covers)

**Location**: `/srv/ace-step/` on gpu-dev-3 (100.116.10.41)
**Type**: Lyrics-to-song with optional reference audio conditioning
**Quality**: HIGH - 48kHz stereo, up to 2 min songs, vocal synthesis
**GPU Memory**: ~9-14 GB

#### Text-Only Generation (no reference audio)
Generates music from tags + lyrics. Produces songs in the requested style but does NOT replicate the original melody.

```bash
ssh 100.116.10.41 "cd /srv/ace-step && \
  LD_LIBRARY_PATH=/usr/lib/wsl/lib:/usr/lib/wsl/drivers \
  .venv/bin/python generate_cli.py \
    --tags 'j-rock, anime opening, electric guitar, powerful drums, energetic vocal' \
    --lyrics 'Your lyrics here...' \
    --duration 120 \
    --title 'My-Song-Title' \
    --output-dir /host/d/Music/ace-step/output/"
```

**Similarity score vs original**: ~80/100 (style match only, not melodic match)

#### Reference Audio Cover (RECOMMENDED for actual covers)
Uses source audio as a reference. This is the closest to what Suno does.

```bash
ssh 100.116.10.41 "cd /srv/ace-step && \
  LD_LIBRARY_PATH=/usr/lib/wsl/lib:/usr/lib/wsl/drivers \
  .venv/bin/python generate_cli.py \
    --tags 'piano ballad, emotional, gentle, acoustic' \
    --lyrics 'Your lyrics here...' \
    --ref-audio /path/to/source.wav \
    --cover-noise-strength 0.2 \
    --audio-cover-strength 1.0 \
    --duration 100 \
    --title 'My-Cover' \
    --output-dir /host/d/Music/ace-step/output/"
```

**Similarity score vs original**: 94-95/100 (strong melodic/rhythmic match)

#### Key Parameters

| Parameter | Range | Default | Effect |
|-----------|-------|---------|--------|
| `--cover-noise-strength` | 0.0-1.0 | 0.5 | 0.0=exact copy, 1.0=fully new. **0.2-0.3 for faithful covers, 0.5 for balanced, 0.7-0.8 for creative reimagining** |
| `--audio-cover-strength` | 0.0-1.0 | 1.0 | How strongly the reference audio influences output. Keep at 1.0 for covers, lower for loose inspiration |
| `--duration` | seconds | - | Duration of output. For covers, match source duration |
| `--steps` | int | - | Inference steps (more = slower but higher quality) |

#### Tested Results (Blue Bird)

| Variant | cover-noise-strength | Score | BPM Match | Duration |
|---------|---------------------|-------|-----------|----------|
| RefCover-Faithful | 0.2 | 95.4 | Perfect (152) | 1:40 (matches source) |
| RefCover-PianoBallad | 0.5 | 95.4 | Perfect (152) | 1:40 |
| RefCover-EDM | 0.8 | 94.6 | Half-time (76) | 1:40 |
| JRock (text-only) | N/A | 80.4 | 119.7 (miss) | 2:00 |
| PianoBallad (text-only) | N/A | ~80 | varies | 2:00 |

**Key finding**: Even at noise=0.8 (very creative), ref-audio covers score 94%+ vs 80% for text-only. **Always use --ref-audio for actual covers.**

#### Output Specs
- Format: WAV, PCM 16-bit
- Sample rate: 48000 Hz
- Channels: Stereo
- Duration: Matches --duration parameter (or source if using ref-audio with appropriate duration)

---

### 2. MusicGen (Facebook)

**Location**: `/srv/music-gen/` on gpu-dev-3, HTTP API on port 8010
**Type**: Text-to-music only (no reference audio)
**Quality**: LOW-MEDIUM - 32kHz mono, max ~41s (2048 tokens)
**GPU Memory**: ~2.5-6 GB

#### Usage

```bash
# Check health
curl -s http://100.116.10.41:8010/health

# Generate
curl -s -X POST http://100.116.10.41:8010/generate \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "upbeat Japanese anime opening, electric guitar, energetic drums", "max_new_tokens": 1024}'
```

#### Limitations
- **No reference audio** - can only describe style via text prompt
- **Max ~41 seconds** at 2048 tokens (20s at 1024)
- **Mono 32kHz** - lower quality than ACE-Step
- **No vocals** - instrumental only
- **Similarity score**: ~82/100 (genre match, not melodic match)

#### When to Use
- Quick instrumental sketches
- Style exploration (what does "anime rock" sound like?)
- NOT suitable for actual covers

---

### 3. Stable Audio Open

**Location**: `/srv/stable-audio/` on gpu-dev-3
**Type**: Text-to-music (diffusion-based)
**Quality**: MEDIUM - 44.1kHz stereo, max 47s
**GPU Memory**: ~6-8 GB

#### Usage

```bash
ssh 100.116.10.41 "cd /srv/stable-audio && \
  source venv/bin/activate && \
  LD_LIBRARY_PATH=/usr/lib/wsl/lib:/usr/lib/wsl/drivers \
  python generate.py 'upbeat j-rock anime opening, electric guitar, drums' 47.0"
```

Output goes to: `/host/d/Music/stable-audio/`

#### Limitations
- **No reference audio** - text prompt only
- **Max 47 seconds**
- **No vocals**
- Good for short atmospheric/ambient pieces
- NOT suitable for actual covers

---

### 4. DiffRhythm

**Location**: `/srv/diffrhythm/` on gpu-dev-3
**Type**: Text + LRC lyrics to song (vocals + instruments)
**Quality**: MEDIUM - 44.1kHz stereo, ~95s songs
**GPU Memory**: ~4-8 GB

#### Usage

```bash
ssh 100.116.10.41 "cd /srv/diffrhythm && \
  LD_LIBRARY_PATH=/usr/lib/wsl/lib:/usr/lib/wsl/drivers \
  bash run_generate.sh --ref-prompt 'j-rock anime opening' \
    --lrc-file /path/to/lyrics.lrc \
    --output /host/d/Music/diffrhythm/output.wav \
    --duration 95"
```

Requires LRC (timestamped lyrics) format:
```
[00:00.00]Habataitara modoranai to itte
[00:05.00]Mezashita no wa aoi aoi ano sora
```

Also supports `--ref-audio-path` for style reference (untested for covers).

#### Limitations
- **No melodic reference conditioning** (text/LRC only, ref-audio is style only)
- Requires LRC format (timestamped lyrics) - **empty lines crash the language detector**
- Only supports Chinese and English text (no Japanese/Korean)
- Output quality varies significantly
- ~15s generation (warm), ~45s cold
- Max 285 seconds
- NOT suitable for actual covers

#### LRC Gotcha
Every LRC line MUST have actual text. Empty lines like `[00:30.00]` will crash with `Exception: Unknown language: `. Use a placeholder like `[00:30.00]...` instead.

---

### 5. HeartMuLa

**Location**: `/srv/heartmula/` on gpu-dev-3
**Type**: Tags + lyrics to song
**Quality**: HIGH - 48kHz stereo, up to 4 min
**GPU Memory**: ~8-12 GB (3B model)

#### Usage

Edit tags and lyrics first:
```bash
# Edit: /srv/heartmula/heartlib/assets/tags.txt
# Edit: /srv/heartmula/heartlib/assets/lyrics.txt
ssh 100.116.10.41 "cd /srv/heartmula && ./generate.sh 'output-name.mp3' 240000"
```

#### Limitations
- **No reference audio** - text/tags only
- Requires editing files on disk (not CLI args)
- Slow generation (~3-5 min for full song)
- Good vocal quality but unpredictable melody
- NOT suitable for actual covers

---

### 6. Suno (via suno_wrapper)

**Location**: `/home/codex/.codex/projects/music-gen/suno_wrapper/`
**Type**: AI music generation service with audio-conditioned covers
**Quality**: HIGHEST - professional quality, full songs with vocals
**Requirements**: Active Suno account with valid auth tokens

#### Cover Workflow

```bash
cd /home/codex/.codex/projects/music-gen
uv run coverctl suno cover /path/to/source.mp3 \
  --tags "anime, j-rock, emotional" \
  --title "Blue Bird Cover" \
  --output-dir data/suno-covers
```

#### Current Status: BROKEN
- **Auth tokens expired** (JWT expired Feb 14, 2026)
- Suno requires browser-based authentication (Clerk)
- The `suno_auth_autofix.py` script on gpu-dev-3 is running but cannot refresh tokens without manual browser login
- To fix: Log into suno.com, extract fresh cookie/JWT, update `~/.env.suno`

#### When Working
- Best quality covers of all models
- Audio-conditioned (uploads source, generates cover)
- Professional vocal and instrumental quality
- Multiple style tags supported

---

### 7. Local Pipeline (coverctl)

**Location**: `/home/codex/.codex/projects/music-gen/`
**Type**: Audio-to-MIDI transcription + arrangement + rendering
**Quality**: LOW (mock transcriber) or MEDIUM (BasicPitch)

#### Usage

```bash
cd /home/codex/.codex/projects/music-gen
uv run coverctl run input.mp3 --style piano --transcriber basicpitch
uv run coverctl run input.mp3 --style orchestra --transcriber basicpitch
```

#### Current Status: PARTIALLY WORKING
- **Mock transcriber**: Works but generates fake C-major scale MIDI (useless for covers)
- **BasicPitch transcriber**: BROKEN - requires Python 3.11 (TensorFlow dependency), current env is Python 3.13
- **Sine renderer**: Works but very low quality (pure sine waves)
- **FluidSynth renderer**: Not available (fluidsynth binary not installed)

#### To Fix
1. Install Python 3.11 or run BasicPitch on gpu-dev-3
2. Install fluidsynth package for proper instrument sounds
3. Add a soundfont (TimGM6mb.sf2 is bundled with pretty_midi)

---

## Audio Similarity Analysis

Tool at: `tools/audio_similarity.py`

### Usage

```bash
cd /home/codex/.codex/projects/music-gen
uv run python tools/audio_similarity.py reference.wav cover.wav
```

### Output (JSON)

```json
{
  "reference": {"path": "...", "duration_s": 99.71, "sample_rate": 48000, "estimated_bpm": 152.0},
  "cover": {"path": "...", "duration_s": 100.0, "sample_rate": 48000, "estimated_bpm": 152.0},
  "metrics": {
    "spectral_similarity": 0.9726,
    "tempo_similarity": 1.0,
    "chroma_similarity": 0.938,
    "energy_similarity": 0.8944
  },
  "overall_cover_score": 95.4,
  "interpretation": "Strong cover match - high melodic/rhythmic similarity"
}
```

### Score Interpretation

| Score | Meaning |
|-------|---------|
| 75-100 | Strong cover match - high melodic/rhythmic similarity |
| 55-74 | Moderate match - recognizable similarities |
| 40-54 | Weak match - some shared characteristics |
| 25-39 | Low similarity - mostly different, similar genre at best |
| 0-24 | Not a cover - unrelated audio |

### Metric Weights
- Chroma (key/melody): 35% - most important for covers
- Spectral (timbre): 30%
- Tempo (rhythm): 20%
- Energy (dynamics): 15%

---

## Recommendations

### For Actual Covers (Suno-like quality)

**Best approach**: ACE-Step with `--ref-audio`

1. Get source audio in WAV format
2. Prepare lyrics (romanji for Japanese songs)
3. Run with low `cover-noise-strength` (0.2-0.3) for faithful covers
4. Use tags to control the style/instrumentation
5. Verify with `audio_similarity.py` (target: 90+ score)

### For Style Variations

Use ACE-Step text-only with different tags:
- `j-rock, anime opening, electric guitar, powerful drums`
- `piano ballad, emotional, gentle, acoustic`
- `electronic, EDM, synth, anime remix`
- `city pop, glossy synths, bass groove, nostalgic`

### For Quick Instrumental Sketches

Use MusicGen HTTP API for fast 20-40s instrumental previews.

### GPU Contention

Only run **one model at a time** on gpu-dev-3. Multiple models competing for the 24GB RTX 3090 will cause OOM errors or extreme slowdowns.

---

## File Locations

| Item | Path |
|------|------|
| Project source | `/home/codex/.codex/projects/music-gen/` |
| ACE-Step | `/srv/ace-step/` (gpu-dev-3) |
| MusicGen server | `/srv/music-gen/` (gpu-dev-3, port 8010) |
| Stable Audio | `/srv/stable-audio/` (gpu-dev-3) |
| DiffRhythm | `/srv/diffrhythm/` (gpu-dev-3) |
| HeartMuLa | `/srv/heartmula/` (gpu-dev-3) |
| Audio output | `/host/d/Music/<model>/` (gpu-dev-3) |
| Similarity tool | `/home/codex/.codex/projects/music-gen/tools/audio_similarity.py` |
| Suno env | `~/.env.suno` |
| Source audio lib | `/host/d/Music/suno/popular-sources/` (gpu-dev-3) |

## LD_LIBRARY_PATH Requirement

ALL GPU models on gpu-dev-3 require:
```bash
export LD_LIBRARY_PATH=/usr/lib/wsl/lib:/usr/lib/wsl/drivers
```
Without this, CUDA will fail with "nvidia-smi has failed" errors.
