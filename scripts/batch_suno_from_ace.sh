#!/bin/bash
# Batch Suno covers from ACE Step outputs
# Processes each ACE Step variant through multiple Suno presets

set -euo pipefail

cd /home/codex/.codex/projects/music-gen
export SUNO_AUTH_TOKEN="$(cat /tmp/suno_jwt_fresh.txt)"

COVERS_DIR="data/anime-covers"

# Suno presets
declare -A PRESETS
PRESETS[rock]="anime, j-rock, powerful, electric guitar, live drums, emotional, cinematic"
PRESETS[orchestral]="anime, orchestral, cinematic, strings, piano, emotional, soundtrack"
PRESETS[city-pop]="anime, city pop, glossy synths, bass groove, nostalgic, bright, polished"
PRESETS[ballad]="anime, emotional ballad, piano, strings, soaring vocal, heartfelt"
PRESETS[edm]="electronic, EDM, synth, upbeat, energetic, remix, powerful"
PRESETS[lofi]="lo-fi, chill, vinyl crackle, relaxing, mellow, study beats"

# Which ACE variant to feed Suno (prefer orchestral, fall back to faithful)
get_suno_source() {
  local slug="$1"
  local base="$COVERS_DIR/$slug/ace-step"

  # Prefer orchestral for variety
  for variant in orchestral faithful subtle; do
    local wavs=("$base/$variant"/*.wav)
    if [[ -f "${wavs[0]}" ]]; then
      echo "${wavs[0]}"
      return 0
    fi
  done
  echo ""
}

# Slugs to process
SLUGS=(
  gurenge kaikai-kitan shinzou-wo-sasageyo haruka-kanata
  go-fighting-dreamers sign-flow we-are-one-piece
  blinding-lights shape-of-you drivers-license believer
)

# Presets per song
PRESET_LIST="rock orchestral city-pop ballad"

for slug in "${SLUGS[@]}"; do
  source_wav=$(get_suno_source "$slug")
  if [[ -z "$source_wav" ]]; then
    echo "[suno-batch] $slug: No ACE Step output found, skipping"
    continue
  fi

  echo "[suno-batch] $slug: Using $source_wav"

  for preset in $PRESET_LIST; do
    out_dir="$COVERS_DIR/$slug/suno/$preset"
    done_marker="$out_dir/.done"

    if [[ -f "$done_marker" ]]; then
      echo "[suno-batch] $slug/$preset: SKIP (already done)"
      continue
    fi

    title="$slug $preset cover"
    tags="${PRESETS[$preset]}"

    echo "[suno-batch] $slug/$preset: generating..."

    # Refresh JWT before each call
    export SUNO_AUTH_TOKEN="$(cat /tmp/suno_jwt_fresh.txt)"

    uv run coverctl suno cover "$source_wav" \
      --tags "$tags" \
      --title "$title" \
      --output-dir "$out_dir" \
      --model chirp-crow \
      --timeout 300 \
      --pre-download-wait 25 \
      --wav 2>&1 | tail -5

    if [[ ${PIPESTATUS[0]} -eq 0 ]]; then
      echo "ok" > "$done_marker"
      echo "[suno-batch] $slug/$preset: ✓"
    else
      echo "[suno-batch] $slug/$preset: FAILED"
    fi
  done
done

echo ""
echo "[suno-batch] Complete!"
