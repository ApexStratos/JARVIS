#!/usr/bin/env bash
# Run JARVIS Nexus in the foreground — macOS/Linux. For Windows, use run.bat.
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
PYTHON=$(command -v python3.13 || command -v python3)
exec "$PYTHON" server.py
