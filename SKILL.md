---
name: MMA-Consensus-Engine
description: A tool to scrape YouTube transcripts, extract MMA betting picks, and generate a weighted consensus report.
---

# MMA Consensus Engine Project

## Overview
Automate the collection of MMA betting analysis from 30+ YouTube channels to generate a weekly consensus report (MLs, Over/Under, Methods of Victory).

## Pipeline Architecture
1. **Scraper (GitHub Actions):** Pulls transcripts from YouTube IDs provided in a `config.json`.
2. **Analysis (Claude API):** Processes transcripts to extract JSON-formatted picks.
3. **Weighting (Python):** Aggregates picks based on defined capper "Trust Scores."
4. **Output:** Generates `consensus.json` which is read by a static frontend.

## Blockers & Strategy
- **YouTube Limits:** Use `youtube-transcript-api` with randomized delays.
- **Unstructured Data:** Use Structured JSON Output in LLM prompts.
- **Hosting:** Static hosting on GitHub Pages; data refreshed by GitHub Actions.

## Weekly Workflow
1. Update `config.json` with new video URLs for the upcoming event.
2. Trigger GitHub Action.
3. View results on the web dashboard.

## Implementation Notes
- The consensus artifact is written to `docs/data.json` (served by GitHub Pages
  from the `docs/` directory alongside `docs/index.html`).
- Trust scores are per-market: each capper carries an `overall`, `underdog`, and
  `favorite` score, and the aggregator picks the one matching how the capper
  framed the bet.
- Transcripts and extractions are cached under `cache/` so re-runs neither
  re-hit YouTube nor re-bill the Claude API.
