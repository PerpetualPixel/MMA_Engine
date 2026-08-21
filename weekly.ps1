# One-button weekly consensus run.
#
# Double-click weekly.bat (or run this script directly) to:
#   1. pull the latest code and config
#   2. discover this week's capper videos (YouTube Data API)
#   3. fetch transcripts and extract picks (Claude API), including every
#      channel's pick from the tracker roundups listed in config.json
#      ("tracker.picks_videos" — paste this week's roundup URL there), plus
#      any cards you pasted into pasted\ for paywalled cappers
#   4. build docs/data.json (the dashboard) and docs/picks.json (the
#      weighted picks feed PerpetualPicks.com reads)
#   5. push both, updating the live site in about a minute
#
# Needs a one-time .env file next to this script with:
#   ANTHROPIC_API_KEY=sk-ant-...
#   YOUTUBE_API_KEY=AIza...
# Optionally, for live moneylines on the dashboard and parlay pricing:
#   ODDS_API_KEY=...
# See README.md "Quick start" for where each key comes from.

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

Write-Host "== Pulling latest code ==" -ForegroundColor Cyan
git pull --ff-only
if ($LASTEXITCODE -ne 0) {
    Fail "git pull failed - fix the error above (uncommitted local changes?) and rerun."
}

if (-not (Test-Path ".venv")) {
    Write-Host "== First run: creating Python environment ==" -ForegroundColor Cyan
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { Fail "Could not create a virtualenv - is Python installed?" }
}

Write-Host "== Installing dependencies ==" -ForegroundColor Cyan
& ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "pip install failed - see the error above." }

Write-Host "== Building the consensus ==" -ForegroundColor Cyan
$env:PYTHONPATH = "src"
& ".venv\Scripts\python.exe" -m mma_engine --config config.json --output docs\data.json
if ($LASTEXITCODE -ne 0) {
    Fail "The pipeline failed (see errors above). Nothing was pushed."
}

Write-Host "== Building the weighted picks feed ==" -ForegroundColor Cyan
& ".venv\Scripts\python.exe" -m mma_engine.weighted_picks --input docs\data.json --output docs\picks.json
if ($LASTEXITCODE -ne 0) {
    Fail "Building picks.json failed (see errors above). Nothing was pushed."
}

Write-Host "== Publishing ==" -ForegroundColor Cyan
git add docs/data.json docs/picks.json
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -m "chore: weekly consensus refresh"
    if ($LASTEXITCODE -ne 0) {
        Fail ("git commit failed - see the error above. If it says 'Author identity unknown', run:`n" +
              '  git config --global user.name "Your Name"' + "`n" +
              '  git config --global user.email "you@example.com"' + "`n" +
              "then rerun weekly.bat.")
    }
    git push
    if ($LASTEXITCODE -ne 0) { Fail "git push failed - see the error above." }
    Write-Host ""
    Write-Host "Done - the dashboard updates in about a minute." -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "Done - no changes since the last run, nothing to publish." -ForegroundColor Green
}
Read-Host "Press Enter to close"
