#!/usr/bin/env bash
# bootstrap_suno_infra.sh — Start all Suno infrastructure and validate auth.
#
# Launches:
#   1. Xvfb + Chrome with CDP (for BrowserOS captcha solving)
#   2. BrowserOS MCP FastAPI server on port 9200
#   3. JWT refresh daemon (keeps SUNO_AUTH_TOKEN alive)
#   4. Auth validation
#
# Usage:
#   bash scripts/bootstrap_suno_infra.sh
#   bash scripts/bootstrap_suno_infra.sh --skip-vnc    # Skip VNC/display setup
#   bash scripts/bootstrap_suno_infra.sh --validate     # Only validate, don't start services

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────
SUNO_WRAPPER_DIR="${SUNO_WRAPPER_DIR:-/home/codex/.codex/projects/suno-wrapper}"
MUSIC_GEN_DIR="${MUSIC_GEN_DIR:-/home/codex/.codex/projects/music-gen}"
ENV_FILE="${HOME}/.env.suno"
JWT_FILE="/tmp/suno_jwt_fresh.txt"
DISPLAY_NUM="${DISPLAY_NUM:-1}"
CHROME_CDP_PORT="${CHROME_CDP_PORT:-9222}"
BROWSEROS_PORT="${BROWSEROS_PORT:-9200}"

# ── Colors ────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[bootstrap]${NC} $*"; }
ok()    { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
fail()  { echo -e "${RED}[✗]${NC} $*"; }

# ── Parse Args ────────────────────────────────────────────────────────
SKIP_VNC=false
VALIDATE_ONLY=false

for arg in "$@"; do
    case "$arg" in
        --skip-vnc)     SKIP_VNC=true ;;
        --validate)     VALIDATE_ONLY=true ;;
        --help|-h)
            echo "Usage: bootstrap_suno_infra.sh [--skip-vnc] [--validate]"
            exit 0
            ;;
    esac
done

# ── Helpers ───────────────────────────────────────────────────────────
is_port_open() {
    ss -tlnp 2>/dev/null | grep -q ":$1 " 2>/dev/null
}

wait_for_port() {
    local port=$1 name=$2 max_wait=${3:-15}
    local waited=0
    while ! is_port_open "$port" && [ "$waited" -lt "$max_wait" ]; do
        sleep 1
        waited=$((waited + 1))
    done
    if is_port_open "$port"; then
        ok "$name listening on port $port"
        return 0
    else
        fail "$name not responding on port $port after ${max_wait}s"
        return 1
    fi
}

# ── Load Credentials ─────────────────────────────────────────────────
info "Loading credentials from $ENV_FILE"
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
    ok "Loaded $ENV_FILE"
else
    fail "$ENV_FILE not found — run browser login first"
    exit 1
fi

# Use fresh JWT if available
if [ -f "$JWT_FILE" ]; then
    export SUNO_AUTH_TOKEN
    SUNO_AUTH_TOKEN="$(cat "$JWT_FILE")"
    ok "Using fresh JWT from $JWT_FILE"
fi

# ── Validate Only Mode ───────────────────────────────────────────────
if $VALIDATE_ONLY; then
    info "Validate-only mode"

    echo ""
    info "=== Suno API Configuration ==="
    echo "  API URL:   https://studio-api.prod.suno.com"
    echo "  Model:     chirp-crow (v5)"
    echo "  Auth:      JWT from $JWT_FILE"
    echo ""

    info "=== Service Status ==="
    is_port_open "$CHROME_CDP_PORT" && ok "Chrome CDP on :$CHROME_CDP_PORT" || warn "Chrome CDP not running on :$CHROME_CDP_PORT"
    is_port_open "$BROWSEROS_PORT"  && ok "BrowserOS MCP on :$BROWSEROS_PORT" || warn "BrowserOS MCP not running on :$BROWSEROS_PORT"
    pgrep -f "refresh_jwt.py" >/dev/null 2>&1 && ok "JWT refresh daemon running" || warn "JWT refresh daemon not running"
    echo ""

    info "=== Auth Check ==="
    cd "$SUNO_WRAPPER_DIR"
    if SUNO_AUTH_TOKEN="${SUNO_AUTH_TOKEN}" uv run python scripts/check_auth.py 2>&1; then
        ok "Suno auth valid"
    else
        fail "Suno auth failed — may need re-login"
        exit 1
    fi
    exit 0
fi

# ── Phase 1: Display + Chrome CDP ────────────────────────────────────
if ! $SKIP_VNC; then
    info "Phase 1: Display + Chrome CDP"

    # Start Xvfb if not running
    if ! pgrep -f "Xvfb :${DISPLAY_NUM}" >/dev/null 2>&1; then
        info "Starting Xvfb on :${DISPLAY_NUM}"
        Xvfb ":${DISPLAY_NUM}" -screen 0 1280x720x24 &
        sleep 1
        ok "Xvfb started"
    else
        ok "Xvfb already running on :${DISPLAY_NUM}"
    fi
    export DISPLAY=":${DISPLAY_NUM}"

    # Start Chrome with CDP if not running
    if ! is_port_open "$CHROME_CDP_PORT"; then
        info "Starting Chrome with CDP on port $CHROME_CDP_PORT"
        CHROME_DATA="/tmp/suno-chrome-cdp"
        mkdir -p "$CHROME_DATA"

        google-chrome-stable \
            --remote-debugging-port="$CHROME_CDP_PORT" \
            --user-data-dir="$CHROME_DATA" \
            --no-first-run \
            --disable-background-networking \
            --disable-sync \
            --window-size=1280,720 \
            --disable-gpu \
            "https://suno.com" &
        sleep 3
    fi
    wait_for_port "$CHROME_CDP_PORT" "Chrome CDP" 10
else
    info "Skipping VNC/display setup (--skip-vnc)"
fi

# ── Phase 2: BrowserOS MCP ───────────────────────────────────────────
info "Phase 2: BrowserOS MCP for captcha solving"

if ! is_port_open "$BROWSEROS_PORT"; then
    info "Starting BrowserOS MCP on port $BROWSEROS_PORT"
    cd "$SUNO_WRAPPER_DIR"
    nohup uv run uvicorn scripts.browseros_mcp_lite:app \
        --host 0.0.0.0 --port "$BROWSEROS_PORT" \
        > /tmp/browseros_mcp.log 2>&1 &
    wait_for_port "$BROWSEROS_PORT" "BrowserOS MCP" 15
else
    ok "BrowserOS MCP already running on port $BROWSEROS_PORT"
fi

# ── Phase 3: JWT Refresh Daemon ──────────────────────────────────────
info "Phase 3: JWT refresh daemon"

if ! pgrep -f "refresh_jwt.py" >/dev/null 2>&1; then
    info "Starting JWT refresh daemon (interval=20min)"
    cd "$SUNO_WRAPPER_DIR"
    nohup uv run python scripts/refresh_jwt.py --loop --interval 20 \
        > /tmp/suno_jwt_refresh.log 2>&1 &
    sleep 2
    if pgrep -f "refresh_jwt.py" >/dev/null 2>&1; then
        ok "JWT refresh daemon started (PID $(pgrep -f 'refresh_jwt.py' | head -1))"
    else
        warn "JWT refresh daemon may not have started — check /tmp/suno_jwt_refresh.log"
    fi
else
    ok "JWT refresh daemon already running (PID $(pgrep -f 'refresh_jwt.py' | head -1))"
fi

# ── Phase 4: Validate Auth ───────────────────────────────────────────
info "Phase 4: Validating Suno auth"

# Reload JWT in case refresh daemon just wrote a fresh one
if [ -f "$JWT_FILE" ]; then
    export SUNO_AUTH_TOKEN
    SUNO_AUTH_TOKEN="$(cat "$JWT_FILE")"
fi

cd "$SUNO_WRAPPER_DIR"
if SUNO_AUTH_TOKEN="${SUNO_AUTH_TOKEN}" uv run python scripts/check_auth.py 2>&1; then
    ok "Suno auth valid"
else
    warn "Auth check failed — JWT may need manual refresh (browser re-login)"
fi

# ── Summary ──────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════${NC}"
echo -e "${GREEN} Suno Infrastructure Ready${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════${NC}"
echo ""
echo "  API URL:        https://studio-api.prod.suno.com"
echo "  Model:          chirp-crow (Suno v5)"
echo "  JWT file:       $JWT_FILE"
echo "  Credentials:    $ENV_FILE"
echo "  Chrome CDP:     localhost:$CHROME_CDP_PORT"
echo "  BrowserOS MCP:  localhost:$BROWSEROS_PORT"
echo "  JWT refresh:    every 20min (PID $(pgrep -f 'refresh_jwt.py' 2>/dev/null | head -1 || echo 'N/A'))"
echo ""
echo "  Usage:"
echo "    source ${MUSIC_GEN_DIR}/suno.env.sh"
echo "    cd ${MUSIC_GEN_DIR}"
echo "    coverctl suno cover /path/to/song.wav --tags 'anime, rock'"
echo "    coverctl anime-pipeline /path/to/songs/ --output-dir data/anime-covers"
echo ""
echo "  Validate:  bash $0 --validate"
echo "  Logs:      /tmp/browseros_mcp.log, /tmp/suno_jwt_refresh.log"
echo ""
