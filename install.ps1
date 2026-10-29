# Installer for Windows. macOS/Linux users: see install.sh instead.
# Run from PowerShell: .\install.ps1
# (If scripts are blocked: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "  J.A.R.V.I.S. Nexus - Installer (Windows)"
Write-Host "  ================================"
Write-Host ""

# When run via `irm ... | iex` there's no local checkout yet - clone one and
# re-run from inside it. Detected by the absence of server.py, so running
# this script directly from an existing clone (.\install.ps1) still works
# unchanged.
if (-not (Test-Path server.py)) {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Host "  [!] git not found - install it first, then re-run this script."
        exit 1
    }
    $target = Join-Path $HOME "jarvis-nexus"
    if (Test-Path (Join-Path $target ".git")) {
        Write-Host "  [~] Found an existing clone at $target - updating it..."
        git -C $target pull --ff-only
    } else {
        Write-Host "  [~] Cloning into $target..."
        git clone --depth 1 https://github.com/ApexStratos/JARVIS.git $target
    }
    Set-Location $target
    & .\install.ps1
    exit
}

$python = Get-Command python3 -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python -ErrorAction SilentlyContinue }
if (-not $python) {
    Write-Host "  [!] Python not found. Install it first: https://python.org/downloads"
    exit 1
}
$py = $python.Source

# Ollama — the free local LLM backend. Installed automatically if missing
# so this script is genuinely the only step.
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Write-Host "  [~] Ollama not found - installing..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id Ollama.Ollama -e --silent
        $env:Path += ";$env:LOCALAPPDATA\Programs\Ollama"
    } else {
        Write-Host "  [!] Couldn't auto-install Ollama (winget not found) - install it manually: https://ollama.com"
        exit 1
    }
}

# Make sure the Ollama daemon is running.
try { Invoke-RestMethod -Uri http://localhost:11434/api/tags -TimeoutSec 2 | Out-Null }
catch {
    Write-Host "  [~] Starting Ollama..."
    Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Seconds 1
        try { Invoke-RestMethod -Uri http://localhost:11434/api/tags -TimeoutSec 2 | Out-Null; break } catch {}
    }
}

# Pull a default model if none exist yet.
$models = & ollama list 2>$null | Select-Object -Skip 1
if (-not $models) {
    Write-Host "  [~] No Ollama models found - pulling llama3.2 (this can take a few minutes)..."
    & ollama pull llama3.2
}

# ffmpeg is a soft dependency — used for audio transcoding on some paths.
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "  [~] ffmpeg not found."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "      Installing via winget..."
        try { winget install --id Gyan.FFmpeg -e --silent } catch {
            Write-Host "  [!] ffmpeg install failed - some audio features will be limited."
        }
    } else {
        Write-Host "      Install manually from https://ffmpeg.org/download.html, or skip it;"
        Write-Host "      most features work without it."
    }
}

Write-Host "  [1/5] Installing Python dependencies..."
& $py -m pip install -q --upgrade pip
& $py -m pip install -q -r requirements.txt

Write-Host "  [2/5] Setting up environment..."
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "        Created .env from .env.example - edit it to add optional keys."
}

Write-Host "  [3/5] Creating output directories..."
New-Item -ItemType Directory -Force -Path "assets/models/kokoro","assets/voices/fish","tts_output","config" | Out-Null

if (-not (Test-Path config/projects.json) -and (Test-Path config/projects.example.json)) {
    Copy-Item config/projects.example.json config/projects.json
}

Write-Host "  [4/5] Downloading voice models (Kokoro ONNX + embeddings)..."
& $py provision_all_voices.py --kokoro-only 2>$null
if ($LASTEXITCODE -ne 0) {
    & $py -c "
try:
    from kokoro_onnx import Kokoro
    print('  Kokoro will auto-download on first synthesis.')
except ImportError:
    print('  [!] kokoro-onnx not installed - check requirements.txt')
"
}

Write-Host "  [5/5] Starting JARVIS..."
$proc = Start-Process -FilePath (Join-Path $PWD "run.bat") -WindowStyle Hidden -PassThru

$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try { Invoke-RestMethod -Uri http://localhost:5001/api/health -TimeoutSec 2 | Out-Null; $ready = $true; break } catch {}
}

Write-Host ""
if ($ready) {
    Write-Host "  JARVIS is running (PID $($proc.Id)) - opening your browser..."
    Start-Process "http://localhost:5001"
} else {
    Write-Host "  [!] JARVIS didn't come up in time - run .\run.bat manually and check the console output."
}
Write-Host ""
Write-Host "  Next time: just run  .\run.bat"
Write-Host ""
Write-Host "  To run automatically at login, see deploy\README.md for Task Scheduler setup."
Write-Host ""
