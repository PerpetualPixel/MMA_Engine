# One-time setup: registers a Windows Task Scheduler job that runs
# weekly.bat's logic on its own, weekly, with nobody at the keyboard.
#
# Run this once, from a PowerShell window (not by double-clicking):
#   .\schedule_weekly.ps1
#
# Combined with "event": {"mode": "auto"} in config.json (the pipeline
# retargets itself to whichever UFC card is soonest, every run — see
# src/mma_engine/auto_event.py), this is what makes the whole thing hands-off:
# no weekly double-click, no manual retarget. You still get to look — every
# run's output lands in logs\weekly_<timestamp>.log — but nothing waits on it.
#
# What it sets up:
#   - A trigger firing every Wednesday at 9 AM (override with -DayOfWeek /
#     -Time), which gives that week's cappers a few days to post previews
#     before a typical Saturday card and leaves you time to look over the
#     dashboard before the weekend.
#   - WakeToRun + StartWhenAvailable, so a sleeping or briefly-off laptop
#     still gets the run (Task Scheduler wakes it, or fires as soon as it's
#     next on) instead of silently skipping the week.
#   - Runs as your own Windows account via S4U (no stored password), so it
#     works whether or not you're logged in at the time.
#
# Undo: Remove-ScheduledTask -TaskName "MMA Engine Weekly" (or delete it from
# Task Scheduler's GUI, under Task Scheduler Library).

param(
    [ValidateSet("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")]
    [string]$DayOfWeek = "Wednesday",
    [string]$Time = "09:00",
    [string]$TaskName = "MMA Engine Weekly"
)

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
$weeklyScript = Join-Path $repoRoot "weekly.ps1"

if (-not (Test-Path $weeklyScript)) {
    Write-Host "Could not find weekly.ps1 next to this script - run it from the repo root." -ForegroundColor Red
    exit 1
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$weeklyScript`" -Unattended" `
    -WorkingDirectory $repoRoot

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DayOfWeek -At $Time

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

# S4U: runs under your account, whether or not you're logged in, without
# storing a password. If your machine's security policy rejects that (some
# managed/work laptops do), re-register with a stored password instead:
#   $cred = Get-Credential
#   Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
#     -Settings $settings -User $cred.UserName -Password $cred.GetNetworkCredential().Password
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "Runs the MMA Engine's weekly consensus build automatically (see weekly.ps1 / README.md)." `
        -Force | Out-Null
} catch {
    Write-Host ""
    Write-Host "Could not register the task: $_" -ForegroundColor Red
    Write-Host "Some managed/work machines block the password-less S4U logon type above." -ForegroundColor Yellow
    Write-Host "See the comment above `$principal in this script for the stored-password alternative." -ForegroundColor Yellow
    Read-Host "`nPress Enter to close"
    exit 1
}

Write-Host ""
Write-Host "Scheduled '$TaskName': every $DayOfWeek at $Time." -ForegroundColor Green
Write-Host "Each run's output lands in logs\weekly_<timestamp>.log."
Write-Host ""
Write-Host "Run it right now to confirm it works: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Check on it any time: Get-ScheduledTaskInfo -TaskName '$TaskName'"
Write-Host "Remove it: Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
Read-Host "`nPress Enter to close"
