#!/usr/bin/env bash
# Starts the local Fish Speech 1.5 API server at http://localhost:8080
# Run once: huggingface-cli download fishaudio/fish-speech-1.5 --local-dir ~/.cache/fish-speech/fish-speech-1.5
set -e

REPO="$HOME/.cache/fish-speech/repo"
CKPT="$HOME/.cache/fish-speech/fish-speech-1.5"
PORT="${FISH_LOCAL_PORT:-8080}"
HOST="${FISH_LOCAL_HOST:-127.0.0.1}"

# ── Clone server repo if missing ────────────────────────────────────────────
if [ ! -d "$REPO/tools" ]; then
  echo "[fish-server] Cloning fish-speech repo..."
  git clone --depth 1 --branch v1.5.0 \
    https://github.com/fishaudio/fish-speech.git "$REPO"
fi

# ── Download checkpoint if missing ──────────────────────────────────────────
DECODER="$CKPT/firefly-gan-vq-fsq-8x1024-21hz-generator.pth"
if [ ! -f "$DECODER" ]; then
  echo "[fish-server] Downloading fish-speech-1.5 checkpoint (~1.5GB)..."
  huggingface-cli download fishaudio/fish-speech-1.5 \
    --local-dir "$CKPT"
fi

# ── Start server ─────────────────────────────────────────────────────────────
echo "[fish-server] Starting at http://$HOST:$PORT"
cd "$REPO"
exec python3.13 -m tools.api_server \
  --llama-checkpoint-path "$CKPT" \
  --decoder-checkpoint-path "$DECODER" \
  --listen "$HOST:$PORT"
