@echo off
rem Double-click me: runs the weekly MMA consensus build. Logic lives in weekly.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0weekly.ps1"
