#!/usr/bin/env bash
# Installer for macOS and Linux. Windows users: see install.ps1 instead.
# Goal: one command, zero follow-up steps — by the end of this script JARVIS
# is running and your browser is open to it.
set -e

PYTHON=${PYTHON:-python3.13}
if ! command -v "$PYTHON" &>/dev/null; then
  PYTHON=python3
fi

OS="$(uname -s)"

echo ""
echo "  J.A.R.V.I.S. Nexus — Installer ($OS)"
echo "  ================================"
echo ""

# When run via `curl ... | bash` there's no local checkout yet — clone one
# and re-run from inside it. Detected by the absence of server.py, so running
# this script directly from an existing clone (bash install.sh) still works
# unchanged.
if [ ! -f server.py ]; then
  if ! command -v git &>/dev/null; then
    echo "  [!] git not found — install it first, then re-run this script."
    exit 1
  fi
  TARGET="$HOME/jarvis-nexus"
  if [ -d "$TARGET/.git" ]; then
    echo "  [~] Found an existing clone at $TARGET — updating it..."
    git -C "$TARGET" pull --ff-only
  else
    echo "  [~] Cloning into $TARGET..."
    git clone --depth 1 https://github.com/ApexStratos/JARVIS.git "$TARGET"
  fi
  cd "$TARGET"
  exec bash install.sh
fi

# ── Ollama — the free local LLM backend. Installed automatically if missing
# so "run this script" is genuinely the only step; both install methods
# below are Ollama's own official ones.
if ! command -v ollama &>/dev/null; then
  echo "  [~] Ollama not found — installing..."
  if [ "$OS" = "Darwin" ] && command -v brew &>/dev/null; then
    brew install ollama
  elif [ "$OS" = "Linux" ]; then
    curl -fsSL https://ollama.com/install.sh | sh
  else
    echo "  [!] Couldn't auto-install Ollama on this setup — install it manually: https://ollama.com"
    exit 1
  fi
fi

# Make sure the Ollama daemon is actually running (needed to pull/use models).
if ! curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
  echo "  [~] Starting Ollama..."
  if [ "$OS" = "Darwin" ] && command -v brew &>/dev/null; then
    brew services start ollama >/dev/null 2>&1 || (ollama serve >/dev/null 2>&1 &)
  else
    (ollama serve >/dev/null 2>&1 &)
  fi
  for i in $(seq 1 15); do
    curl -sf http://localhost:11434/api/tags >/dev/null 2>&1 && break
    sleep 1
  done
fi

# Pull a default model if none exist yet — a fresh Ollama install has zero
# models, which would otherwise make the chat silently fail on first use.
if [ "$(ollama list 2>/dev/null | tail -n +2 | wc -l | tr -d ' ')" = "0" ]; then
  echo "  [~] No Ollama models found — pulling llama3.2 (this can take a few minutes)..."
  ollama pull llama3.2
fi

# ── ffmpeg — used for audio transcoding (radio streaming, some TTS paths).
# Soft dependency: several features work without it, so a missing package
# manager or failed install is a warning, not a hard stop.
if ! command -v ffmpeg &>/dev/null; then
  echo "  [~] ffmpeg not found — installing..."
  if [ "$OS" = "Darwin" ]; then
    if command -v brew &>/dev/null; then
      brew install ffmpeg || echo "  [!] ffmpeg install failed — some audio features will be limited."
    else
      echo "  [!] Homebrew not found (https://brew.sh) — install ffmpeg manually, or skip it;"
      echo "      most features work without it."
    fi
  elif [ "$OS" = "Linux" ]; then
    if command -v apt-get &>/dev/null; then
      sudo apt-get update -qq && sudo apt-get install -y ffmpeg || \
        echo "  [!] ffmpeg install failed — some audio features will be limited."
    elif command -v dnf &>/dev/null; then
      sudo dnf install -y ffmpeg || echo "  [!] ffmpeg install failed — some audio features will be limited."
    elif command -v pacman &>/dev/null; then
      sudo pacman -S --noconfirm ffmpeg || echo "  [!] ffmpeg install failed — some audio features will be limited."
    else
      echo "  [!] No supported package manager found (apt/dnf/pacman) — install ffmpeg manually,"
      echo "      or skip it; most features work without it."
    fi
  fi
fi

echo "  [1/5] Installing Python dependencies..."
"$PYTHON" -m pip install -q --upgrade pip
"$PYTHON" -m pip install -q -r requirements.txt

echo "  [2/5] Setting up environment..."
if [ ! -f .env ]; then
  cp .env.example .env
  echo "        Created .env from .env.example — edit it later to add optional keys."
fi

echo "  [3/5] Creating output directories..."
mkdir -p assets/models/kokoro assets/voices/fish tts_output config

if [ ! -f config/projects.json ]; then
  cp config/projects.example.json config/projects.json 2>/dev/null || true
fi

echo "  [4/5] Downloading voice models (Kokoro ONNX + embeddings)..."
"$PYTHON" provision_all_voices.py --kokoro-only 2>/dev/null || \
"$PYTHON" -c "
try:
    from kokoro_onnx import Kokoro
    # Auto-downloads to ~/.cache/kokoro-onnx on first import
    print('  Kokoro will auto-download on first synthesis.')
except ImportError:
    print('  [!] kokoro-onnx not installed — check requirements.txt')
"

echo "  [5/5] Starting JARVIS..."
chmod +x run.sh 2>/dev/null || true
nohup ./run.sh >/tmp/jarvis-nexus-install.log 2>&1 &
JARVIS_PID=$!

for i in $(seq 1 30); do
  if curl -sf http://localhost:5001/api/health >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo ""
if curl -sf http://localhost:5001/api/health >/dev/null 2>&1; then
  echo "  ✓ JARVIS is running (PID $JARVIS_PID) — opening your browser..."
  if [ "$OS" = "Darwin" ]; then
    open http://localhost:5001
  elif command -v xdg-open &>/dev/null; then
    xdg-open http://localhost:5001 >/dev/null 2>&1 &
  else
    echo "    Open this yourself: http://localhost:5001"
  fi
else
  echo "  [!] JARVIS didn't come up in time — check /tmp/jarvis-nexus-install.log, then run ./run.sh manually."
fi
echo ""
echo "  Next time: just run  ./run.sh"
echo ""
if [ "$OS" = "Darwin" ]; then
  echo "  To run 24/7 as a background service instead of ./run.sh each time:"
  echo "    bash manage_nexus.sh install-agents && bash manage_nexus.sh start"
elif [ "$OS" = "Linux" ]; then
  echo "  To run 24/7 as a background service (systemd):"
  echo "    see deploy/jarvis-nexus.service.example"
fi
echo ""
