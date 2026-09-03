@echo off
rem One-time setup: registers weekly.bat to run automatically every week via
rem Windows Task Scheduler. Logic lives in schedule_weekly.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0schedule_weekly.ps1"
