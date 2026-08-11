# MMA Consensus Engine

Pulls YouTube transcripts from a roster of MMA betting cappers, extracts their
picks with the Claude API, and aggregates them into a **trust-weighted
consensus** rendered by a static dashboard.

```
config.json ─▶ channel discovery ─▶ transcripts ─▶ Claude extraction ─▶ weighted aggregation ─▶ docs/data.json ─▶ docs/index.html
              (RSS, finds videos)   (cached)        (structured JSON)     (per-capper trust)                       (GitHub Pages)
```

---

## Quick start

```bash
git clone https://github.com/miguelsgarcia4/MMA_Engine.git
cd MMA_Engine

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env       # then paste your key into .env — it is gitignored
```

Point it at the event in `config.json` (discovery finds the videos itself):

```json
"event": { "name": "UFC 320" },
"settings": {
  "discovery": {
    "enabled": true,
    "lookback_days": 14,
    "max_videos_per_channel": 3,
    "title_contains": ["ufc 320", "ufc320"]
  }
}
```

Run it:

```bash
PYTHONPATH=src python -m mma_engine --discover-only   # dry run: what would it pull?
PYTHONPATH=src python -m mma_engine                   # writes docs/data.json
python -m http.server -d docs 8000                    # open http://localhost:8000
```

> The dashboard fetches `data.json`, so it must be served over HTTP — opening
> `docs/index.html` directly from disk is blocked by the browser.

### Useful flags

| Flag | Effect |
| --- | --- |
| `--discover-only` | Dry run: print the videos discovery finds, then exit. No transcripts, no API calls. |
| `--skip-extraction` | Fetch and cache transcripts only. No Claude API calls, no cost. |
| `--discover` / `--no-discover` | Force channel discovery on or off, overriding the config. |
| `--no-cache` | Ignore cached transcripts/extractions and redo everything. |
| `--config` / `--output` | Point at a different config or output path. |
| `--roster-from URL` | Derive trust scores from a tracker results video. See below. |
| `--roster-mode` | `accumulate` (post-event reviews) or `replace` (period recap). |
| `--apply-roster` | Merge the extracted roster into `config.json`. |
| `-v` | Debug logging. |

Exit codes: `0` success, `1` every video failed, `2` bad or empty config.

---

## Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | yes | Claude API key ([console](https://platform.claude.com/settings/keys)). |
| `MMA_MODEL` | no | Overrides `settings.model` (default `claude-opus-5`). |
| `MMA_EFFORT` | no | Overrides `settings.effort` (`low`/`medium`/`high`/`xhigh`/`max`). |

**Locally:** put the key in `.env` — `.gitignore` already excludes it, and the
pipeline loads it via `python-dotenv`.

**In GitHub Actions:** add it as a repository secret named `ANTHROPIC_API_KEY`
under *Settings → Secrets and variables → Actions → New repository secret*. Never
commit the key, and never paste it into `config.json`.

If you use the `ant` CLI, `ant auth login` also works — the SDK picks up the
stored profile with no env var set.

---

## How the weighting works

Every pick contributes a weight:

```
weight = trust_for(role) × (confidence / 10)
```

- **`confidence`** (1–10) is extracted from the capper's own language: a lean is
  3–5, a confident pick 6–8, a stated best bet or big unit play 9–10.
- **`role`** is how the capper framed the side — `underdog`, `favorite`, or
  `unknown`.
- **`trust_for(role)`** selects that capper's underdog score, favorite score, or
  overall score to match.

A market's consensus percentage is an option's share of the weight cast in that
market:

```
consensus_pct = option_weight ÷ market_total_weight × 100
```

So two 9.0-trust cappers at 8–9 confidence (weight 15.3) outrank one 9.0-trust
capper at 6 confidence (weight 5.4) → 73.9% / 26.1%.

### Where trust scores come from

They're plain numbers in `config.json` and nothing in the code hardcodes a
capper, so you can always edit them by hand. But the better source is measured
results — see **Trust scores from a tracker channel** below.

The starting values were inferred from the tracked-performance lists this
project began with (top predictors 7.5 overall, others 5.0; underdog and
favorite specialists 9.0 in their lane, reduced outside it). Those are a
placeholder for real ROI data, not a substitute for it.

---

## Trust scores from a tracker channel

Channels like [@UFCPredictionsTracker](https://www.youtube.com/@UFCPredictionsTracker)
publish recaps ranking MMA prediction channels by correct-pick rate and ROI,
split into overall / favorite / underdog — the same three axes this project
weights on. `--roster-from` turns one of those videos into capper entries with
measured trust scores.

```bash
# 1. Reset the baseline from a long-period recap (writes a proposal only)
PYTHONPATH=src python -m mma_engine \
  --roster-from https://www.youtube.com/watch?v=rLxl9yy3Tbc \
  --roster-mode replace

# 2. Review roster_proposal.json, then apply it
PYTHONPATH=src python -m mma_engine \
  --roster-from https://www.youtube.com/watch?v=rLxl9yy3Tbc \
  --roster-mode replace --apply-roster

# 3. After each event, fold in that card's review (accumulate is the default)
PYTHONPATH=src python -m mma_engine \
  --roster-from https://www.youtube.com/watch?v=EVENT_REVIEW_ID --apply-roster
```

### The two modes

| Mode | Use for | Effect |
| --- | --- | --- |
| `replace` | A long-period recap (6 months, 10 months) | Discards the stored record; this video alone sets the scores. |
| `accumulate` *(default)* | A post-event review | Adds this card's picks to the running sample and recomputes. |

Use `replace` for a period recap **because it covers the same picks the
per-event videos already contributed** — pooling both would double-count. The
normal rhythm is one `replace` to set a baseline, then `accumulate` after every
card. Re-running the same video is a no-op, so a repeated run can't inflate
anyone's sample.

### How a figure becomes a weight

```
raw   = 5.0 + (roi_percent / 20.0) × 5.0        # +20% ROI → 10.0, 0% → 5.0
trust = 5.0 + (raw − 5.0) × n / (n + 50)        # shrink toward neutral
```

ROI is preferred over correct-pick rate — profitability is what the weighting
cares about, and a dog specialist can be very profitable while hitting under
50%. Win rate is the fallback when a video reports only that.

The second line is the important one. **A big number over a tiny sample is
noise**, so scores are pulled toward neutral by their sample size:

| Record | Trust |
| --- | --- |
| +55% ROI over 9 picks | 7.1 |
| +14% ROI over 420 picks | 8.1 |
| +26% ROI over 140 picks | 9.8 |
| −18% ROI over 300 picks | 1.1 |

Tune `ROI_AT_MAX_TRUST` and `PRIOR_PICKS` in `src/mma_engine/roster.py` if you
disagree with the curve. A capper with no category-specific number inherits
their overall score rather than getting an invented specialty rating.

Extracted records are stored under each capper's `tracked` key in
`config.json`, including which videos have been applied — that's what makes
re-runs idempotent and the scores auditable.

---

## Channel discovery

Rather than pasting eight video URLs every week, discovery reads each capper's
public channel feed:

```
https://www.youtube.com/feeds/videos.xml?channel_id=UC...
```

No API key and no quota — it's the same feed any RSS reader uses, carrying the
last ~15 uploads with IDs, titles, and publish dates. Channels written as
`@handle` are resolved to a channel ID once and cached in `cache/channels.json`.

A video is picked up when it clears three filters:

| Setting | Filter |
| --- | --- |
| `lookback_days` | Published within this many days. |
| `title_contains` | Title contains **any** listed substring (case-insensitive). Empty = no title filter. |
| `max_videos_per_channel` | Cap per channel, newest first. |

`title_contains` takes a list because cappers spell events inconsistently —
`["ufc 320", "ufc320"]` catches both. Widen it if a capper titles videos by main
event instead (e.g. add `"ankalaev"`).

Set `"discover": false` on a capper to keep them configured but excluded from the
sweep. Anything you list by hand in `videos` is always used and wins over a
discovered duplicate.

**Always dry-run first:** `--discover-only` (or the *discover_only* checkbox on
the Action) prints exactly what would be processed, with per-channel failures,
before you spend a cent.

---

## Weekly workflow

1. Update `event.name` and `settings.discovery.title_contains` in `config.json`
   for the new event.
2. Run the **Build consensus** action with *discover_only* ticked to confirm it
   finds the right videos.
3. Run it again unticked. It also runs Fridays at 15:00 UTC.
4. The action commits a refreshed `docs/data.json`; the dashboard picks it up.

To publish the dashboard: *Settings → Pages → Source: Deploy from a branch →
`main` / `/docs`*.

---

## Adding a capper

```json
{
  "id": "new_capper",
  "name": "New Capper",
  "channel_url": "https://www.youtube.com/@NewCapper",
  "trust": { "overall": 5.0, "underdog": 5.0, "favorite": 5.0 }
}
```

Then reference `"capper_id": "new_capper"` from a video entry. Start new
accounts at 5.0 and adjust once you have a sample of their results.

---

## Settings reference (`config.json` → `settings`)

| Key | Default | Meaning |
| --- | --- | --- |
| `model` | `claude-opus-5` | Claude model used for extraction. |
| `effort` | `high` | Reasoning effort. `medium` is cheaper and usually fine. |
| `max_tokens` | `20000` | Output cap per extraction call. |
| `transcript_languages` | `["en"]` | Preferred transcript languages, in order. |
| `min_delay_seconds` / `max_delay_seconds` | `4` / `12` | Randomized pause between YouTube requests. |
| `max_transcript_chars` | `60000` | Transcripts longer than this are chunked. |
| `min_confidence` | `1` | Drop picks below this confidence from the consensus. |
| `use_cache` | `true` | Reuse cached transcripts and extractions. |
| `discovery.enabled` | `true` | Pull videos from capper channels automatically. |
| `discovery.lookback_days` | `14` | Only consider uploads this recent. |
| `discovery.max_videos_per_channel` | `3` | Cap per channel, newest first. |
| `discovery.title_contains` | `[]` | Title must contain any of these (case-insensitive). |

---

## Caching and cost

`cache/transcripts/` and `cache/extractions/` are keyed by video ID and
gitignored. A re-run after a partial failure re-fetches and re-bills only the
videos that are actually new. The static system prompt carries a cache
breakpoint, so repeated extractions in one run read it from the prompt cache
instead of re-billing it.

To force a clean run: `--no-cache`, or `rm -rf cache/`.

---

## Project layout

```
config.json                  # the only file you edit weekly
src/mma_engine/
  config.py                  # config loading, validation, URL → video ID
  discover.py                # channel RSS discovery, handle → channel ID
  roster.py                  # tracker-video → measured capper trust scores
  transcripts.py             # YouTube fetching, caching, jittered delays
  extract.py                 # Claude structured-output extraction, chunking
  normalize.py               # fighter-name and selection matching
  aggregate.py               # trust-weighted consensus math
  pipeline.py                # orchestration + CLI
docs/index.html              # static dashboard (no build step, no CDN)
docs/data.json               # generated output
tests/test_aggregate.py      # normalization + aggregation tests
tests/test_discover.py       # feed parsing, filtering, merge behavior
tests/test_roster.py         # trust arithmetic, pooling, config merge
.github/workflows/consensus.yml
```

## Tests

```bash
PYTHONPATH=src python -m pytest -q
```

No network or API key required — the tests cover name normalization, market
grouping, the weighting math, RSS feed parsing, discovery filtering, and the
tracker-derived trust arithmetic against constructed fixtures.

---

## Known limitations

- **Surname matching.** Fights are keyed on fighter surnames so different
  spellings group together. Two fighters sharing a surname on one card would
  collide; rare, but worth knowing.
- **Transcript quality.** Auto-generated captions mangle fighter names. The
  extraction prompt corrects obvious cases, but a garbled name can produce a
  stray fight entry.
- **YouTube blocking.** Datacenter IPs get rate-limited or blocked. Delays and
  caching mitigate it; if the runner starts failing, `youtube-transcript-api`
  supports proxy configuration.
- **Discovery is title-based.** It cannot tell a betting preview from a recap or
  a vlog except by title text and date. Dry-run with `--discover-only` before
  each event and adjust `title_contains` — a wrong filter silently produces an
  empty or off-topic consensus.
- **Tracker extraction is one model reading one auto-generated transcript**
  full of channel names, and channel names are exactly what auto-captions
  mangle. Always review `roster_proposal.json` before `--apply-roster`, and
  sanity-check that ROI figures landed in the right category.
- **Handle resolution scrapes HTML.** Resolving `@handle` → channel ID parses the
  channel page, which YouTube can change. Pin `channel_id` in `config.json` to
  skip it entirely (Artem's is already pinned as an example).
- **Extraction is a judgment call.** Confidence and underdog/favorite framing are
  inferred from how the capper talks. Spot-check the per-capper reasoning shown
  in the dashboard before trusting a number.

This tool summarizes other people's opinions. It is not betting advice.
