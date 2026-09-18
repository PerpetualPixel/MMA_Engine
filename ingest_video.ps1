# Paste a YouTube URL, get its picks on the dashboard.
#
# Double-click ingest_video.bat (or run this script with the URL as its
# argument) to:
#   1. ask for the video's URL, if it wasn't given
#   2. download the video, screenshot every unique frame, and read the picks
#      printed on each one — a pick card, a best-bets slide, a bet slip, or a
#      tracker-style board (src/mma_engine/screen_picks.py)
#   3. attribute them to the channel that posted the video (matched against
#      config.json, minted at neutral trust if unknown)
#   4. rebuild docs/data.json and docs/picks.json with those picks folded in
#      alongside this week's roundup and any pasted cards
#   5. remember the URL in config.json (so weekly.bat keeps the picks, for
#      free, from screens\<video_id>.json) and push everything
#
# The same one-time .env as weekly.bat (ANTHROPIC_API_KEY at least — the
# metadata lookup and download need no YouTube key). See README.md
# "Ingest any picks video from its screenshots".
#
# If yt-dlp can't download the video (blocked IP, age gate), screenshot the
# picks by hand into a folder and run:
#   .venv\Scripts\python.exe -m mma_engine --picks-from-video URL --video-frames C:\path\to\shots --remember-videos --no-discover --output docs\data.json

param(
    [string]$Url = "",
    [string]$Capper = ""
)

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

if (-not $Url) {
    $Url = Read-Host "Paste the YouTube URL of the picks video"
}
$Url = $Url.Trim()
if (-not $Url) { Fail "No URL given - nothing to ingest." }

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

$env:PYTHONPATH = "src"

Write-Host "== Reading the video off its screen and rebuilding the consensus ==" -ForegroundColor Cyan
$arguments = @("-m", "mma_engine", "--config", "config.json", "--no-discover",
               "--picks-from-video", $Url, "--remember-videos", "--output", "docs\data.json")
if ($Capper) { $arguments += @("--video-capper", $Capper) }
& ".venv\Scripts\python.exe" @arguments
if ($LASTEXITCODE -ne 0) {
    git checkout -- docs/data.json docs/picks.json 2>$null
    Fail ("The pipeline failed (see errors above). Nothing was pushed, and the " +
          "live dashboard still shows the last good run.")
}

Write-Host "== Building the weighted picks feed ==" -ForegroundColor Cyan
& ".venv\Scripts\python.exe" -m mma_engine.weighted_picks --input docs\data.json --output docs\picks.json
if ($LASTEXITCODE -ne 0) {
    Fail "Building picks.json failed (see errors above). Nothing was pushed."
}

Write-Host "== Publishing ==" -ForegroundColor Cyan
git add docs/data.json docs/picks.json config.json screens
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "Done - the video added nothing new (already ingested, or no picks on screen)." -ForegroundColor Green
    Read-Host "Press Enter to close"
    exit 0
}
git commit -m "chore: ingest picks video $Url"
if ($LASTEXITCODE -ne 0) {
    Fail ("git commit failed - see the error above. If it says 'Author identity unknown', run:`n" +
          '  git config --global user.name "Your Name"' + "`n" +
          '  git config --global user.email "you@example.com"' + "`n" +
          "then rerun ingest_video.bat.")
}
git push
if ($LASTEXITCODE -ne 0) {
    Write-Host "Push rejected - the remote moved. Rebasing onto it and retrying." -ForegroundColor Yellow
    git pull --rebase -X theirs origin main
    if ($LASTEXITCODE -ne 0) {
        Fail "Rebase failed - run 'git status' and resolve it by hand. This run's commit is local, so nothing is lost."
    }
    git push
    if ($LASTEXITCODE -ne 0) { Fail "git push failed again - see the error above." }
}
Write-Host "Done - the dashboard updates in about a minute." -ForegroundColor Green
Read-Host "Press Enter to close"
