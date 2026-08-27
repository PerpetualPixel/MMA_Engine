@echo off
rem Double-click me: lists what the weekly build would pull, without paying for
rem any of it. Logic lives in preview.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0preview.ps1"
