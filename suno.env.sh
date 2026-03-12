#!/usr/bin/env bash
# Source this file to load Suno credentials into the shell environment.
# Usage: source suno.env.sh
#
# This reads from ~/.env.suno (the canonical credential store) and
# /tmp/suno_jwt_fresh.txt (kept alive by refresh_jwt.py --loop).
#
# The JWT refresh daemon should be running:
#   cd /home/codex/.codex/projects/suno-wrapper
#   nohup uv run python scripts/refresh_jwt.py --loop --interval 20 &

set -a  # auto-export all variables

# ── Load persistent credentials from ~/.env.suno ──
if [[ -f "$HOME/.env.suno" ]]; then
    while IFS='=' read -r key value; do
        [[ -z "$key" || "$key" == \#* ]] && continue
        export "$key=$value"
    done < "$HOME/.env.suno"
fi

# ── Override JWT with the freshest copy from /tmp ──
if [[ -f /tmp/suno_jwt_fresh.txt ]]; then
    SUNO_AUTH_TOKEN="$(cat /tmp/suno_jwt_fresh.txt)"
    export SUNO_AUTH_TOKEN
fi

set +a

# ── Verify ──
if [[ -n "$SUNO_AUTH_TOKEN" ]]; then
    echo "Suno env loaded: AUTH_TOKEN=${#SUNO_AUTH_TOKEN} chars, PROJECT_ID=${SUNO_PROJECT_ID:-unset}"
else
    echo "WARNING: SUNO_AUTH_TOKEN is empty. Check ~/.env.suno and /tmp/suno_jwt_fresh.txt"
fi
