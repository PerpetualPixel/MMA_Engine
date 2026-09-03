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
#
# -Unattended: for Windows Task Scheduler (see schedule_weekly.ps1). Skips the
# "Press Enter to close" pauses, which would otherwise hang forever with no
# one there to press Enter, and writes a transcript to logs\ so a run nobody
# watched still leaves something to check afterward.

param(
    [switch]$Unattended
)

$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

if ($Unattended) {
    New-Item -ItemType Directory -Force -Path "logs" | Out-Null
    $logPath = "logs\weekly_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss")
    Start-Transcript -Path $logPath -Append | Out-Null
}

function Pause-UnlessUnattended {
    if (-not $Unattended) {
        Read-Host "Press Enter to close"
    }
}

function Fail([string]$message) {
    Write-Host ""
    Write-Host $message -ForegroundColor Red
    Restore-Sleep
    Pause-UnlessUnattended
    if ($Unattended) { Stop-Transcript | Out-Null }
    exit 1
}

# With open search on, a run can cover 80+ videos and run 20-30+ minutes of
# transcript fetches and extraction calls unattended. Windows suspending the
# machine mid-run freezes the whole process — every timer, every open
# connection — for however long the machine is asleep; on wake it looks
# exactly like a hang, because for that stretch it was one. SetThreadExecutionState
# is the standard way to tell Windows "don't sleep while I hold this" without
# needing admin rights or touching the user's power plan; ES_CONTINUOUS keeps
# the request in effect until cleared, ES_SYSTEM_REQUIRED is system sleep only
# (the display is free to turn off). Every exit path below — success, the
# no-op "nothing changed" branch, and every Fail() — restores normal sleep
# behaviour before the script ends, since `exit` doesn't reliably run a
# try/finally in PowerShell and this needs to work on every path regardless.
$stayAwake = $false
try {
    Add-Type -Name Power -Namespace Win32 -MemberDefinition @'
[DllImport("kernel32.dll", CharSet = CharSet.Auto, SetLastError = true)]
public static extern uint SetThreadExecutionState(uint esFlags);
'@
    $ES_CONTINUOUS = [uint32]"0x80000000"
    $ES_SYSTEM_REQUIRED = [uint32]"0x00000001"
    [Win32.Power]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED) | Out-Null
    $stayAwake = $true
} catch {
    Write-Host "Could not disable sleep for this run - if the machine sleeps, the run freezes until it wakes." -ForegroundColor Yellow
}

function Restore-Sleep {
    if ($script:stayAwake) {
        [Win32.Power]::SetThreadExecutionState([uint32]"0x80000000") | Out-Null
    }
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
# config.json is included because "event": {"mode": "auto"} lets the pipeline
# retarget it in place (new event name, discovery keywords, cleared roundup
# URL) before it even runs — that retarget needs to be committed too, or next
# week's run starts from a config the last run already moved past.
git add docs/data.json docs/picks.json config.json
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
    if ($LASTEXITCODE -ne 0) {
        # The remote moved while this run was working (a merged PR, another
        # machine). Replay this commit on top of it and push again. -X theirs
        # settles docs/data.json and docs/picks.json in favour of the payload
        # this run just built, which is the newer of the two by definition.
        Write-Host "Push rejected - the remote moved. Rebasing onto it and retrying." -ForegroundColor Yellow
        git pull --rebase -X theirs origin main
        if ($LASTEXITCODE -ne 0) {
            Fail ("Rebase failed - run 'git status' and resolve it by hand. " +
                  "Your consensus is committed locally, so nothing is lost.")
        }
        git push
        if ($LASTEXITCODE -ne 0) { Fail "git push failed again - see the error above." }
    }
    Write-Host ""
    Write-Host "Done - the dashboard updates in about a minute." -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "Done - no changes since the last run, nothing to publish." -ForegroundColor Green
}
Restore-Sleep
Pause-UnlessUnattended
if ($Unattended) { Stop-Transcript | Out-Null }
