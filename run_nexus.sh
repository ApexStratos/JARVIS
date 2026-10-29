#!/bin/zsh
# JARVIS Nexus startup script — called by launchd com.local.jarvis-nexus.
# Script-relative paths so this works wherever the repo was cloned, on any Mac.
DIR="$(cd "$(dirname "${0:A}")" && pwd)"
cd "$DIR" || exit 1
PYTHON=$(command -v python3.13 || command -v python3)
exec "$PYTHON" server.py
