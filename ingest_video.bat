@echo off
rem Double-click me (or drag a YouTube URL or a downloaded video file onto me):
rem reads a picks video off its screen and adds its picks to the dashboard.
rem Logic lives in ingest_video.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ingest_video.ps1" %*
