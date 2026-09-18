@echo off
rem Double-click me (or drag a YouTube URL onto me): reads a picks video off
rem its screen and adds its picks to the dashboard. Logic lives in ingest_video.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ingest_video.ps1" %*
