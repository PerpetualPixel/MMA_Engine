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

cp .env.example .env       # then paste both keys into .env — it is gitignored
```

Two free-to-obtain keys go in `.env`:

1. **`ANTHROPIC_API_KEY`** — [platform.claude.com/settings/keys](https://platform.claude.com/settings/keys).
   Pays per extraction call (the only part of this that costs money to run).
2. **`YOUTUBE_API_KEY`** — [console.cloud.google.com](https://console.cloud.google.com):
   create a project → *APIs & Services → Library* → enable **YouTube Data API
   v3** → *Credentials → Create credentials → API key*. Free, no billing setup;
   a weekly run uses ~16 of the 10,000 free daily quota units. (YouTube shut
   down the keyless RSS feeds this used to use — see **Channel discovery**.)

**Windows one-button run:** once `.env` exists, just double-click
**`weekly.bat`** — it pulls the latest code, discovers this week's videos,
builds the consensus, and pushes the updated dashboard. Everything below is
the manual/step-by-step equivalent.

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
| `YOUTUBE_API_KEY` | yes, for channel discovery | Free YouTube Data API v3 key (see **Quick start**). Without it, discovery falls back to YouTube's RSS feeds, which are discontinued and 404. |
| `MMA_MODEL` | no | Overrides `settings.model` (default `claude-opus-5`). |
| `MMA_EFFORT` | no | Overrides `settings.effort` (`low`/`medium`/`high`/`xhigh`/`max`). |
| `MMA_PROXY_ENABLED` | no | Overrides `settings.proxy.enabled` (`true`/`false`). Only relevant if you set up the optional unattended GitHub Actions path; local runs don't need it. |
| `WEBSHARE_PROXY_USERNAME` / `WEBSHARE_PROXY_PASSWORD` | only for the optional unattended path, `settings.proxy.provider` = `webshare` | Proxy credentials, **not** your Webshare account login. See **Optional: fully unattended runs on GitHub Actions** below. |
| `MMA_PROXY_URL` | only for the optional unattended path, `settings.proxy.provider` = `generic` | A full proxy URL, e.g. `http://user:pass@host:port`, for any other residential/rotating proxy provider. |

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

Rather than pasting eight video URLs every week, discovery lists each capper
channel's recent uploads through the **YouTube Data API v3** (the free
`YOUTUBE_API_KEY` from Quick start). Listing one channel's uploads costs 1
quota unit of the 10,000 you get daily, so a weekly run over 8 channels uses
a fraction of a percent of the free allowance. Channels written as `@handle`
are resolved to a channel ID once (a single `channels.list` call) and cached
in `cache/channels.json`; a pinned `channel_id` in `config.json` needs no
lookup at all.

> **Why an API key is now required:** this originally used YouTube's keyless
> per-channel RSS feeds (`/feeds/videos.xml?channel_id=UC...`). As of August
> 2026 that endpoint returns 404 for every channel — including the largest on
> the platform, from residential IPs and plain browsers — so YouTube appears
> to have discontinued it. The RSS code path still exists as an automatic
> fallback when no key is set, but expect it to fail until/unless YouTube
> brings the feeds back.

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

## Weekly workflow (run it locally — one button, no cost)

GitHub's shared `ubuntu-latest` runners sit on cloud-provider IP ranges, and
YouTube blocks transcript fetching from those outright — not a rate limit,
confirmed via `youtube-transcript-api`'s own `RequestBlocked` error in live
runs. Your own computer isn't on a blocked range, so the free path is to run
it there.

**On Windows, the whole weekly run is one double-click: `weekly.bat`.** It
pulls the latest code, installs anything missing, discovers this week's
videos, fetches transcripts, extracts picks, builds `docs/data.json`, and
commits + pushes it — the dashboard updates itself a minute later. If
anything fails, it stops and shows the error instead of pushing.

The only thing the button doesn't do is retarget the event. When a new card
is coming up, edit two lines in `config.json` first (`event.name` and
`settings.discovery.title_contains`), then press the button.

The manual equivalent, on any OS:

1. Update `event.name` and `settings.discovery.title_contains` in `config.json`
   for the new event.
2. `PYTHONPATH=src python -m mma_engine --discover-only` — confirm it finds
   the right videos.
3. `PYTHONPATH=src python -m mma_engine` — writes `docs/data.json`.
4. Commit and push `docs/data.json`; the dashboard (GitHub Pages) picks it up.

Takes a couple of minutes. `.github/workflows/consensus.yml` has no
`schedule:` trigger — only `workflow_dispatch` (manual, from the Actions tab)
— specifically so it doesn't run automatically on GitHub's runners and fail
every week for the reason above.

To publish the dashboard: *Settings → Pages → Source: Deploy from a branch →
`main` / `/docs`*.

---

## Optional: fully unattended runs on GitHub Actions (needs a paid proxy)

Skip this section unless you want the schedule to run itself with zero
weekly effort from you. It costs money and isn't required — the section
above is the default, free way to run this.

To make GitHub's cloud runners look like a normal visitor instead of a
blocked datacenter, the pipeline supports routing through a proxy
(`settings.proxy` in `config.json`, credentials from env vars — see
`src/mma_engine/proxy.py`). **The critical detail:** use a **residential**
proxy plan. YouTube's block targets cloud/datacenter IP ranges (that's the
documented failure `youtube-transcript-api` reports from GitHub's runners),
so a free-trial or Datacenter-tier proxy just swaps one datacenter IP for
another — it's residential exit IPs that make the difference, and they're
what `youtube-transcript-api`'s own docs recommend for exactly this error.

### Setup

1. Sign up for [Webshare](https://www.webshare.io/)'s **Residential** plan
   specifically (not the free trial, not Datacenter) — `youtube_transcript_api`
   has first-class built-in support for it.
2. In the dashboard, under *Proxy → Connection*, set **Connection Method** to
   **Backbone Connection** and confirm it shows a working proxy row under
   **Username/Password** auth (not just the same handful of datacenter IPs
   from a free trial). Copy that username and password — not your account
   login.
3. Add them as repo secrets: *Settings → Secrets and variables → Actions → New
   repository secret* → `WEBSHARE_PROXY_USERNAME` and `WEBSHARE_PROXY_PASSWORD`.
   Also add `YOUTUBE_API_KEY` (same key as your local `.env`) so discovery
   works in CI — the workflow passes all three through.
4. Sanity-check from your own machine before touching CI (the RSS feeds are
   dead, so test against a watch page instead — the thing the transcript
   fetcher actually reads):
   ```bash
   curl -sI --proxy "http://USERNAME:PASSWORD@p.webshare.io:80/" \
     "https://www.youtube.com/watch?v=dQw4w9WgXcQ" | head -1
   ```
   `HTTP/... 200` means the proxy exits through an IP YouTube serves
   normally; a 4xx/5xx or an error page means the plan still isn't giving
   you residential IPs.
5. Uncomment a `schedule:` trigger in `.github/workflows/consensus.yml` (see
   the comment left in its place) and set it to whenever you want the run to
   fire — the `Build consensus` and `Extract roster from a tracker video`
   steps already set `MMA_PROXY_ENABLED=true` and pass the secrets through;
   `config.json`'s `settings.proxy.enabled` stays `false` so local runs are
   still unaffected.

Using a different residential proxy provider instead of Webshare? Set
`settings.proxy.provider` to `"generic"` in `config.json` and add one secret,
`MMA_PROXY_URL` (a full `http://user:pass@host:port` string), instead of the
two Webshare ones.

Without valid credentials, a run fails fast with a clear
`WEBSHARE_PROXY_USERNAME / WEBSHARE_PROXY_PASSWORD are not set` error rather
than the confusing IP-block failures seen before — so it's obvious what's
missing if it's ever misconfigured.

---

## Integrating with other systems (e.g. PerpetualPicks.com)

`docs/data.json` is the entire output of a run and the thing GitHub Pages
serves — anything that can fetch a URL can read it:

```
https://<your-username>.github.io/MMA_Engine/data.json
```

It refreshes every time the Action runs, so a puller on a similar or looser
schedule always sees the latest event's consensus. The shape is:

```jsonc
{
  "schema_version": 1,           // bump on any breaking shape change
  "generated_at": "2026-08-08T15:04:12+00:00",
  "event": { "name": "UFC 320", "date": "" },
  "totals": { "fights": 12, "picks": 84, "cappers": 8, "videos": 8 },
  "fights": [
    {
      "fight_id": "...",
      "display": "Ankalaev vs Pereira",
      "fighter_a": "Ankalaev", "fighter_b": "Pereira",
      "pick_count": 8, "capper_count": 8,
      "markets": [
        {
          "bet_type": "moneyline",           // or method_of_victory / over_under / round / prop
          "label": "Moneyline",
          "total_weight": 41.2,
          "options": [
            {
              "selection": "Ankalaev",
              "consensus_pct": 73.9,          // this option's share of market_weight
              "weight": 30.5,
              "pick_count": 6,
              "avg_confidence": 7.8,
              "cappers": [
                { "id": "artem_mma", "name": "Artem MMA", "confidence": 8,
                  "trust": 7.5, "role": "favorite", "odds": -150,
                  "stake": 2.0, "reasoning": "...", "video_url": "https://youtu.be/..." }
              ]
            }
          ]
        }
      ]
    }
  ],
  "sources": [ /* one row per video processed, incl. failures */ ]
}
```

For a MMA-specific feed into an external algorithm, the two fields worth
pulling per fight/market are `options[].selection` and `options[].consensus_pct`
— the trust-weighted probability this project exists to produce. `weight` and
`pick_count` are there if you want to apply your own confidence threshold
(e.g. ignore any option under 3 picks) before importing it.

If PerpetualPicks.com needs push delivery instead of pulling the static file
(a webhook, a different schema, authentication), that's a small addition to
the `Commit updated data.json` step in `consensus.yml` — happy to wire it up
once you know what shape it expects on the other end.

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
| `proxy.enabled` | `false` | Route YouTube requests through a proxy. Only used for the optional unattended path — see **Optional: fully unattended runs on GitHub Actions**. |
| `proxy.provider` | `webshare` | `webshare` (built-in support) or `generic` (any provider via `MMA_PROXY_URL`). |

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
weekly.bat / weekly.ps1      # Windows one-button run: pull, build, push
src/mma_engine/
  config.py                  # config loading, validation, URL → video ID
  discover.py                # upload discovery: Data API primary, RSS fallback
  roster.py                  # tracker-video → measured capper trust scores
  transcripts.py             # YouTube fetching, caching, jittered delays
  extract.py                 # Claude structured-output extraction, chunking
  normalize.py               # fighter-name and selection matching
  aggregate.py               # trust-weighted consensus math
  proxy.py                   # optional proxy for GitHub Actions' cloud IPs
  pipeline.py                # orchestration + CLI
docs/index.html              # static dashboard (no build step, no CDN)
docs/data.json               # generated output — also the integration feed
tests/test_aggregate.py      # normalization + aggregation tests
tests/test_discover.py       # feed parsing, filtering, merge behavior
tests/test_roster.py         # trust arithmetic, pooling, config merge
tests/test_proxy.py          # proxy config from env vars
tests/test_config.py         # settings defaults, merging, env overrides
.github/workflows/consensus.yml
```

## Tests

```bash
PYTHONPATH=src python -m pytest -q
```

No network or API key required — the tests cover name normalization, market
grouping, the weighting math, Data API and RSS response parsing, discovery
filtering, and the tracker-derived trust arithmetic against constructed
fixtures.

---

## Known limitations

- **Surname matching.** Fights are keyed on fighter surnames so different
  spellings group together. Two fighters sharing a surname on one card would
  collide; rare, but worth knowing.
- **Transcript quality.** Auto-generated captions mangle fighter names. The
  extraction prompt corrects obvious cases, but a garbled name can produce a
  stray fight entry.
- **YouTube blocks transcript fetching from cloud/datacenter IPs** (GitHub
  Actions' shared runners included) — confirmed in live runs via
  `youtube-transcript-api`'s `RequestBlocked` error, whose own docs name
  cloud IPs as the cause. Discovery is immune (the Data API works from
  anywhere), but transcripts aren't. A local run from your own computer
  avoids this entirely — see **Weekly workflow** above. For fully unattended
  cloud runs, see **Optional: fully unattended runs on GitHub Actions**,
  which needs a paid *Residential* proxy plan — a Datacenter-tier proxy just
  swaps one blocked IP class for another.
- **YouTube discontinued its channel RSS feeds** (observed Aug 2026: 404 on
  every channel, everywhere). Discovery therefore requires the free
  `YOUTUBE_API_KEY`; the RSS path remains only as an automatic fallback in
  case the feeds return.
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
