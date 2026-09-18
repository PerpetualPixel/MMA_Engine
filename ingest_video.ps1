# Paste a YouTube URL — or pick a video file you downloaded — and get its
# picks on the dashboard.
#
# Double-click ingest_video.bat (or drag a YouTube link or a video file onto
# it, or run this script with either as its argument) to:
#   1. ask what to read: a YouTube URL, the path to a video file, or press
#      Enter with nothing typed to browse for a file with a file dialog
#   2. cut every unique frame from it (downloading it first if it is a URL)
#      and read the picks printed on each one — a pick card, a best-bets
#      slide, a bet slip, or a tracker-style board (src/mma_engine/screen_picks.py)
#   3. attribute them to the channel that posted the video (matched against
#      config.json, minted at neutral trust if unknown) — for a file, the
#      optional URL you give next says whose video it is
#   4. rebuild docs/data.json and docs/picks.json with those picks folded in
#      alongside this week's roundup and any pasted cards
#   5. remember it in config.json (so weekly.bat keeps the picks, for free,
#      from screens\<video_id>.json) and push everything
#
# The same one-time .env as weekly.bat (ANTHROPIC_API_KEY at least). See
# README.md "Ingest any picks video from its screenshots".
#
# When the URL download fails (YouTube answering 403 usually means yt-dlp is
# stale, which this script upgrades every run), download the video with any
# tool you like and hand the file to this script instead — nothing is
# uploaded anywhere; only the individual frames are read.

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
    $Url = Read-Host "Paste the YouTube URL, or the path to a video file you downloaded (Enter to browse)"
}
$Url = $Url.Trim().Trim('"')
$VideoFile = ""
if (-not $Url) {
    # Nothing typed: open the Windows file picker.
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object System.Windows.Forms.OpenFileDialog
        $dialog.Title = "Pick the video to read"
        $dialog.Filter = "Video files (*.mp4;*.mkv;*.webm;*.mov;*.avi;*.m4v)|*.mp4;*.mkv;*.webm;*.mov;*.avi;*.m4v|All files (*.*)|*.*"
        $dialog.InitialDirectory = [Environment]::GetFolderPath("MyVideos")
        if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
            $VideoFile = $dialog.FileName
        }
    } catch {
        Write-Host "Could not open a file dialog - type the path instead." -ForegroundColor Yellow
    }
    if (-not $VideoFile) { Fail "Nothing chosen - nothing to ingest." }
} elseif (Test-Path -LiteralPath $Url -PathType Leaf) {
    $VideoFile = (Resolve-Path -LiteralPath $Url).Path
    $Url = ""
}

if ($VideoFile) {
    Write-Host "Reading the file: $VideoFile"
    # The file says nothing about who posted it. A URL does; it is optional.
    $Url = (Read-Host "YouTube URL of that video, so its picks go to the right channel (Enter to skip)").Trim()
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
# yt-dlp specifically is upgraded every run: YouTube changes its player
# every few weeks and an older yt-dlp answers with "HTTP Error 403" on the
# video download, which is the one dependency here that rots on a schedule.
& ".venv\Scripts\python.exe" -m pip install --quiet --upgrade yt-dlp
if ($LASTEXITCODE -ne 0) { Write-Host "Could not upgrade yt-dlp - continuing with the installed version." -ForegroundColor Yellow }

$env:PYTHONPATH = "src"

# Retarget (event.mode "auto") and commit it FIRST, exactly as weekly.ps1
# does: the ingest below is the part that can fail, and a failed run must
# not leave config.json's rewrite sitting uncommitted to block the next
# `git pull --ff-only`.
Write-Host "== Checking for a new event ==" -ForegroundColor Cyan
& ".venv\Scripts\python.exe" -m mma_engine.auto_event --config config.json
git add config.json
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -m "chore: auto-retarget to next event"
    if ($LASTEXITCODE -ne 0) { Fail "git commit failed - see the error above." }
    git push
    if ($LASTEXITCODE -ne 0) {
        git pull --rebase -X theirs origin main
        if ($LASTEXITCODE -ne 0) { Fail "Rebase failed - run 'git status' and resolve it by hand." }
        git push
        if ($LASTEXITCODE -ne 0) { Fail "git push failed again - see the error above." }
    }
}

Write-Host "== Reading the video off its screen and rebuilding the consensus ==" -ForegroundColor Cyan
$arguments = @("-m", "mma_engine", "--config", "config.json", "--no-discover",
               "--remember-videos", "--output", "docs\data.json")
if ($Url) { $arguments += @("--picks-from-video", $Url) }
if ($VideoFile) { $arguments += @("--video-file", $VideoFile) }
if ($Capper) { $arguments += @("--video-capper", $Capper) }
& ".venv\Scripts\python.exe" @arguments
if ($LASTEXITCODE -ne 0) {
    # Throw away the half-built output AND the remembered URL: a video that
    # could not be read is not worth carrying into the weekly run, and a
    # dirty config.json would block the next `git pull --ff-only`.
    git checkout -- docs/data.json docs/picks.json config.json 2>$null
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
$what = if ($Url) { $Url } else { Split-Path -Leaf $VideoFile }
git commit -m "chore: ingest picks video $what"
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
