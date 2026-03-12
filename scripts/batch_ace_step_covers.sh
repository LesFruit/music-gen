#!/bin/bash
# Batch ACE Step cover generation on gpu-dev-3
# Runs faithful (0.2), orchestral (0.4), and subtle (0.1) variants for each song

set -euo pipefail

HOST="100.116.10.41"
ACE_DIR="/srv/ace-step"
LD_LIB="/usr/lib/wsl/lib:/usr/lib/wsl/drivers"
SRC_DIR="/host/d/Music/suno/popular-sources"
OUT_DIR="/tmp/ace-step-anime/output"
LOCAL_OUT="/home/codex/.codex/projects/music-gen/data/anime-covers"

# Song definitions: slug|source_file|tags|lyrics
declare -a SONGS=(
  "gurenge|Demon-Slayer-Gurenge-LiSA.wav|anime, j-rock, powerful, dramatic, emotional, LiSA|kawaranai mono nante nai nda yo furikaeru bakka no michijya"
  "kaikai-kitan|Jujutsu-Kaisen-Opening-Kaikai-Kitan.wav|anime, j-rock, fast, rhythmic, dark, mysterious, Eve|itsuka kieta hikari ima mo sagashiteru"
  "shinzou-wo-sasageyo|Attack-on-Titan-Shinzou-wo-Sasageyo.wav|anime, epic, powerful, dramatic, choir, marching|sasageyo sasageyo shinzou wo sasageyo"
  "haruka-kanata|ASIAN KUNG-FU GENERATION - Haruka Kanata.wav|anime, j-rock, energetic, upbeat, nostalgic, guitar|fumikomu ze akuseru kake hiki wa nai sa"
  "go-fighting-dreamers|FLOW - Go!!! (Music Video).wav|anime, j-rock, energetic, upbeat, rock, shounen|we are fighting dreamers takami wo mezashite"
  "sign-flow|Naruto Shippuden Opening 6 ｜ Sign by FLOW.wav|anime, j-rock, emotional, dramatic, powerful|i realize the screaming pain hearing loud in my brain"
  "we-are-one-piece|One-Piece-We-Are-Opening.wav|anime, j-pop, adventure, upbeat, bright, shounen|arittake no yume wo kaki atsume sagashi mono sagashi ni yuku no sa one piece"
  "blinding-lights|The-Weeknd-Blinding-Lights-official-audio.wav|synthwave, 80s, pop, retro, nostalgic, driving|i been tryna call i been on my own for long enough"
  "shape-of-you|Ed-Sheeran-Shape-of-You-official-audio.wav|pop, tropical house, groovy, catchy, romantic|im in love with the shape of you"
  "drivers-license|Olivia-Rodrigo-drivers-license-official-audio.wav|pop, emotional, piano, heartbreak, ballad|i got my drivers license last week just like we always talked about"
  "believer|Imagine-Dragons-Believer-official-audio.wav|rock, anthemic, powerful, drums, motivational|first things first i ma say all the words inside my head"
)

# Variant definitions: name|noise_strength|extra_tags
declare -a VARIANTS=(
  "faithful|0.2|"
  "orchestral|0.4|orchestral, cinematic, strings, piano, brass, emotional, soundtrack, epic"
  "subtle|0.1|faithful cover, add subtle violin, gentle strings background, minimal changes"
)

run_ace_step() {
  local slug="$1" src="$2" tags="$3" lyrics="$4" variant="$5" noise="$6" extra_tags="$7"

  local out_subdir="$LOCAL_OUT/$slug/ace-step/$variant"
  local done_marker="$out_subdir/.done"

  if [[ -f "$done_marker" ]]; then
    echo "[batch] $slug/$variant: SKIP (already done)"
    return 0
  fi

  mkdir -p "$out_subdir"

  # Build tags
  local final_tags="$tags"
  if [[ -n "$extra_tags" ]]; then
    final_tags="$extra_tags"
  fi

  local title="${slug}-${variant}"

  echo "[batch] $slug/$variant (noise=$noise)..."

  local cmd="cd $ACE_DIR && LD_LIBRARY_PATH=$LD_LIB .venv/bin/python generate_cli.py \
    --tags '$final_tags' \
    --lyrics '$lyrics' \
    --ref-audio '$SRC_DIR/$src' \
    --cover-noise-strength $noise \
    --audio-cover-strength 1.0 \
    --duration 120 \
    --title '$title' \
    --output-dir '$OUT_DIR'"

  local output
  output=$(ssh "$HOST" "$cmd" 2>&1) || {
    echo "[batch] $slug/$variant FAILED:"
    echo "$output" | tail -5
    return 1
  }

  # Parse OUTPUT: line
  local remote_path
  remote_path=$(echo "$output" | grep "^OUTPUT:" | head -1 | sed 's/^OUTPUT://' | tr -d ' ')

  if [[ -z "$remote_path" ]]; then
    echo "[batch] $slug/$variant: No OUTPUT line found"
    echo "$output" | tail -10
    return 1
  fi

  # Download
  local filename
  filename=$(basename "$remote_path")
  scp "$HOST:$remote_path" "$out_subdir/$filename" 2>/dev/null
  echo "ok" > "$done_marker"
  echo "[batch] $slug/$variant: $filename ✓"
}

echo "=========================================="
echo " ACE Step Batch Cover Generation"
echo " Songs: ${#SONGS[@]} | Variants: ${#VARIANTS[@]}"
echo " Total jobs: $(( ${#SONGS[@]} * ${#VARIANTS[@]} ))"
echo "=========================================="
echo ""

completed=0
failed=0
skipped=0

for song_def in "${SONGS[@]}"; do
  IFS='|' read -r slug src tags lyrics <<< "$song_def"

  for var_def in "${VARIANTS[@]}"; do
    IFS='|' read -r variant noise extra_tags <<< "$var_def"

    if run_ace_step "$slug" "$src" "$tags" "$lyrics" "$variant" "$noise" "$extra_tags"; then
      if [[ -f "$LOCAL_OUT/$slug/ace-step/$variant/.done" ]]; then
        ((completed++))
      else
        ((skipped++))
      fi
    else
      ((failed++))
    fi
  done
done

echo ""
echo "=========================================="
echo " ACE Step Batch Complete"
echo " Completed: $completed | Skipped: $skipped | Failed: $failed"
echo "=========================================="
