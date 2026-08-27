# Dry run: what would this week's build pull in, and what would it cost?
#
# Double-click preview.bat (or run this script directly) to list every video
# the run would process — the roster channels' uploads and, when
# settings.discovery.search is on, whatever else YouTube turns up for the
# event. Nothing is fetched and nothing is extracted, so this costs nothing:
# it is the look-before-you-pay step, since every video listed here is a
# Claude extraction that weekly.bat would pay for.
#
# Read the "YouTube search" block in the output. If it is full of videos that
# are not predictions, tighten settings.discovery.search.queries rather than
# lowering max_results — the queries decide what is found, the cap only
# decides how much of it gets paid for.
#
# Same one-time .env as weekly.bat: see README.md "Quick start".

$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

function Fail([string]$message) {
    Write-Host ""
    Write-Host $message -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}

if (-not (Test-Path ".env")) {
    Fail "No .env file found. Copy .env.example to .env and add your keys first."
}

if (-not (Test-Path ".venv")) {
    Write-Host "== First run: creating Python environment ==" -ForegroundColor Cyan
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { Fail "Could not create a virtualenv - is Python installed?" }
}

Write-Host "== Installing dependencies ==" -ForegroundColor Cyan
& ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "pip install failed - see the error above." }

Write-Host "== What this week's run would pull (nothing is extracted) ==" -ForegroundColor Cyan
$env:PYTHONPATH = "src"
& ".venv\Scripts\python.exe" -m mma_engine --config config.json --discover-only
$found = $LASTEXITCODE

Write-Host ""
if ($found -eq 0) {
    Write-Host "Nothing was fetched or extracted - no cost. Run weekly.bat to build it for real." -ForegroundColor Green
} else {
    Write-Host ("No videos matched. Widen settings.discovery.lookback_days, adjust " +
                "title_contains to match the titles above, or add search queries.") -ForegroundColor Yellow
}
Read-Host "Press Enter to close"
