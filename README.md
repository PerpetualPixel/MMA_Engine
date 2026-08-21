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
git clone https://github.com/PerpetualPixel/MMA_Engine.git
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

Optionally, a third:

3. **`ODDS_API_KEY`** — [the-odds-api.com](https://the-odds-api.com), free tier.
   Adds current moneyline prices to the dashboard and lets the Parlay Builder
   price a ticket. One request per run against a 500/month allowance. Leave it
   out and everything still works — legs just fall back to the odds cappers
   quoted in their videos.

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
| `--picks-from-text CAPPER_ID=FILE` | Extract picks from a text file you pasted yourself. Repeatable. See **Paywalled cards** below. |
| `--no-pasted-picks` | Skip the `pasted/` folder for this run. |
| `--picks-from-tracker URL` | Ingest a tracker roundup — one video carrying every channel's pick. Repeatable. See below. |
| `--no-tracker-picks` | Skip the roundups listed in `config.json` for this run. |
| `--apply-tracker-cappers` | Write channels first seen in a roundup into `config.json` at neutral trust. |
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
| `ODDS_API_KEY` | no | [The Odds API](https://the-odds-api.com) key for live moneylines (see **Live odds** below). Absent = no live prices, quoted odds still shown. |
| `MMA_LIVE_ODDS_ENABLED` | no | Overrides `settings.live_odds.enabled` (`true`/`false`). Set `0` to skip the odds request for a run. |
| `MMA_MODEL` | no | Overrides `settings.model` (default `claude-opus-5`). |
| `MMA_EFFORT` | no | Overrides `settings.effort` (`low`/`medium`/`high`/`xhigh`/`max`). |
| `MMA_PROXY_ENABLED` | no | Overrides `settings.proxy.enabled` (`true`/`false`). Only relevant if you set up the optional unattended GitHub Actions path; local runs don't need it. |
| `WEBSHARE_PROXY_USERNAME` / `WEBSHARE_PROXY_PASSWORD` | only for the optional unattended path, `settings.proxy.provider` = `webshare` | Proxy credentials, **not** your Webshare account login. See **Optional: fully unattended runs on GitHub Actions** below. |
| `MMA_PROXY_URL` | only for the optional unattended path, `settings.proxy.provider` = `generic` | A full proxy URL, e.g. `http://user:pass@host:port`, for any other residential/rotating proxy provider. |
| `MMA_TRANSCRIPT_COOKIES_FILE` | no | Path to a `cookies.txt` for the age-restricted fallback (see **Age-restricted videos** below). Setting it also turns the fallback on. |
| `MMA_TRANSCRIPT_COOKIES_ENABLED` | no | Overrides `settings.transcript_cookies.enabled` (`true`/`false`). |

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

### Everyone's picks from one video (tracker roundups)

The same channel also posts a **pre-event roundup**: a video that walks the
card fight by fight and reports which prediction channels are on which
fighter — often 150+ of them. That is by far the widest sample available in a
week, and most of those channels have no entry here and no video this pipeline
could read. `tracker.picks_videos` ingests it:

```jsonc
"tracker": {
  "picks_videos": ["https://youtu.be/ROUNDUP_ID"]   // this week's roundup
}
```

Every run then reads it alongside the per-capper videos. For a one-off, or to
try a roundup without editing the config:

```bash
PYTHONPATH=src python -m mma_engine \
  --picks-from-tracker https://youtu.be/ROUNDUP_ID
```

A roundup line is a thinner thing than a capper's own video, and is treated as
one:

- It states no conviction, price, or reasoning, so it enters at a neutral
  confidence (`settings.tracker_picks.confidence`, 5/10) instead of a
  made-up number, and is tagged `via tracker` on the dashboard.
- **A capper's own video always wins.** If a channel is both in the roundup
  and had its own video extracted this run, its roundup line for that fight is
  dropped — one channel, one vote, cast by the richer source.
- Channels with no `config.json` entry count as one unweighted voice each
  (trust 5.0) rather than being thrown away. Their ids are derived from the
  channel name, so they stay stable across runs without being written
  anywhere; `--apply-tracker-cappers` writes them into `config.json` if you
  want to hand-edit their trust or add a channel URL later.
- Names are matched against each capper's `name` and `aliases`, with one edit
  of caption slack, so a garbled "Funky Pick" still reaches Funky Picks.

The ESPN card filter runs afterwards as always, so a roundup that also covers
next week's card contributes nothing off-event. Flags: `--no-tracker-picks`
skips the configured roundups for one run; `settings.tracker_picks.enabled`
turns them off permanently.

---

## Paywalled cards (`pasted/`)

Cappers increasingly post their full card to Patreon and leave a two-play
teaser on YouTube. The pipeline reads the teaser, records two picks where the
capper made twelve, and says nothing about it — a high-trust voice quietly
shrinks to a fraction of its weight, skewed toward whichever plays were loud
enough to give away.

The roundup above covers part of this: a paywalled capper still appears on the
tracker's slide, so their side reaches the consensus even when their upload
doesn't say. But a roundup line is *who*, never *how sure*. When you subscribe
to a capper yourself, paste their card in instead:

```
pasted/funky_picks.txt          # named for the capper id...
pasted/Funky Picks.txt          # ...or their name, or any listed alias
```

The file holds whatever they wrote. An optional first line naming the source
is kept as the pick's link:

```
https://www.patreon.com/posts/12345678

Main event: Hernandez by decision, 2 units. The wrestling gap is...
```

Every run reads the folder through the same extractor a transcript goes
through, so these picks arrive with real confidence, stated odds, and the
capper's own reasoning, and count as fully as a pick from a video. For a
one-off outside the folder:

```bash
PYTHONPATH=src python -m mma_engine --picks-from-text funky_picks=card.txt
```

Three things worth knowing:

- **`pasted/` is gitignored** (the folder and its README stay tracked, nothing
  dropped in does). It holds someone else's paid writing, which is never ours
  to publish. Nothing in the pipeline fetches from Patreon or anywhere else —
  a person puts the text in the file, and should paste only what they are
  entitled to read.
- **A pasted card supersedes that capper's video** for any fight it covers.
  The paste is the full card; the video was the teaser. Their roundup line
  defers to both.
- **Files go stale.** One untouched for longer than
  `settings.pasted_picks.max_age_days` (14) is skipped and reported in the run
  summary, so last month's card can't quietly keep voting. Extractions are
  cached against a hash of the text, so re-running is free and editing the
  file re-reads it.

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

## Live odds

With an `ODDS_API_KEY` set, each run fetches the current **moneyline** market
for the card from [The Odds API](https://the-odds-api.com) and stamps a price
onto every bout it can match:

```json
"live_odds": {
  "source": "the-odds-api",
  "fetched_at": "2026-08-14T04:30:00+00:00",
  "a": { "american": -456, "decimal": 1.2193, "books": 7, "best_american": -444 },
  "b": { "american": 344,  "decimal": 4.44,   "books": 7, "best_american": 361 }
}
```

`a` and `b` follow that fight's own `fighter_a` / `fighter_b`, so a consumer
never has to re-match names. The headline `american` is the **median** across
books — one book hanging a stale number shouldn't move the shown price —
while `best_american` keeps the longest price on offer for anyone shopping
the line.

The dashboard shows these as green pills on the Moneylines tab and uses them
to price parlay legs, multiplying the legs' decimal prices into a ticket
price.

### What this does and doesn't cover

The feed carries **h2h (moneyline) only** for MMA. Method of victory, rounds
and props are not in it at any tier, so those legs keep falling back to the
prices the cappers themselves quoted in their videos. Every price on the
dashboard is therefore tagged with its source:

| Tag | Meaning |
| --- | --- |
| `live` | Current market median, fetched at the timestamp in the page header. Moneylines only. |
| `quoted` | Median of the prices the backing cappers said out loud in their videos. A snapshot from whenever the video went up — **not** a live number. |
| `—` | Nobody priced this leg. It is shown as unpriced and sits out of the parlay total, rather than being estimated. |

Double chance legs are always unpriced. Nothing quotes "wins by KO or
decision" as one line, and combining the two method prices doesn't work:
each already includes its book's margin, so adding the implied probabilities
counts the vig twice. Measured on the UFC 330 card, all 8 derivable double
chances came out *shorter* than the same fighter's own moneyline — impossible
for a subset of "wins" — and three implied a probability above 1.0. A missing
number is recoverable; a confidently wrong one isn't.

### Cost and failure behaviour

One request per run against the free tier's 500/month. Set
`MMA_LIVE_ODDS_ENABLED=0` to skip it for a run.

Fail-open, exactly like the ESPN card fetch: no key, an unreachable host, a
spent quota, or an event the feed doesn't carry all mean "no live prices this
run" — never a failed run. Two fighters whose surnames collide (a
Nurmagomedov–Nurmagomedov bout) are skipped rather than risk hanging one
side's price on the other.

---

## The official card is the boundary

Capper videos are not neatly scoped to one event. A single upload will cover
this weekend's UFC card, next week's Fight Night and a Contender Series bout
in the same breath, and auto-captions invent pairings that were never fights
at all — one bout can surface as a dozen phantom matchups once every capper
mangles the names differently.

So the event's official card decides what is in the payload. Every run reads
the card from **ESPN's MMA scoreboard** (`site.web.api.espn.com`, no key
needed) for the event named in `config.json` → `event.name`, matches each
consensus fight against it on surnames, and:

| Outcome | What happens |
| --- | --- |
| Matches a bout on the card | Kept, tagged `card_status: "on_card"`, given the bout's `card_order`, and renamed to ESPN's clean spellings |
| ESPN marks the bout canceled, or it was on the card last run and is gone now | Kept, tagged `card_status: "cancelled"` — a cancellation is something to show, not to hide |
| On the card but nobody picked it | Appended as a pickless fight, so the dashboard lists the whole card |
| Anything else | **Dropped.** Not in `data.json`, not in `picks.json`, not selectable as a parlay leg |

The headline totals (`totals.fights` / `picks` / `cappers` / `videos`) are
recounted after the drop, so the numbers describe the card you're looking at
rather than everything the transcripts mentioned.
`event.card.off_card_dropped` records how many were removed.

Fail-open, like everything else here: if ESPN is unreachable or the event
name matches nothing, **nothing is dropped**. A run with no card has no basis
on which to call a fight off-card, so it keeps everything and leaves
`card_status` unset rather than guessing. Check the run log for
`No ESPN card found matching ...` if the dashboard suddenly shows more fights
than the card has — the usual cause is `event.name` in `config.json` not
matching ESPN's title for the event.

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
videos, fetches transcripts, extracts picks, builds `docs/data.json` (the
dashboard) and `docs/picks.json` (the weighted picks feed for
PerpetualPicks.com), and commits + pushes both — the live site updates
itself a minute later. If anything fails, it stops and shows the error
instead of pushing.

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

## Age-restricted videos

Some cappers post age-restricted uploads, and YouTube gates those videos'
captions behind a signed-in, 18+ account. The anonymous transcript endpoint
can't read them, so by default they're logged as `transcript_failed`
(`AgeRestricted`) and simply left out of the consensus — the same is true of
the occasional `PoTokenRequired` video.

Because these are public videos you're allowed to watch, the fix is to
authenticate as **yourself** rather than to circumvent anything: when cookies
are configured, those specific failures retry once through
[`yt-dlp`](https://github.com/yt-dlp/yt-dlp) using your own YouTube cookies,
which proves your age to YouTube and lets it hand back the captions it already
would in your browser. Every other video stays on the faster anonymous path,
and a run with no cookies configured behaves exactly as before.

**Turn it on** in `config.json`, pointing at whichever cookie source you have:

```jsonc
"settings": {
  "transcript_cookies": {
    "enabled": true,
    "from_browser": "",          // a browser name to read cookies directly...
    "file": "cookies.txt"        // ...or a path to an exported cookies.txt
  }
}
```

- `file` — a Netscape-format `cookies.txt` exported from a logged-in session.
  **This is the recommended option on Windows** (see the Chrome note below).
  Install a "Get cookies.txt LOCALLY" browser extension, sign into YouTube,
  export from `youtube.com`, and save it as `cookies.txt` next to `weekly.bat`
  (it's `.gitignore`d — a cookies file is a live session token, treat it like
  an API key). A relative path resolves from the repo root the pipeline runs
  in. For unattended/GitHub Actions runs, write the file from a repo secret and
  point `MMA_TRANSCRIPT_COOKIES_FILE` at it (which also flips `enabled` on).
- `from_browser` — a browser profile `yt-dlp` reads directly (`chrome`,
  `firefox`, `edge`, `brave`, …). Close the browser first, since some OSes lock
  the cookie database. If both fields are set, `from_browser` wins.

> **Windows + Chrome/Edge:** Chrome 127+ (and Edge) encrypt cookies with
> App-Bound Encryption that only the browser itself can decrypt, so
> `from_browser: "chrome"` fails with `Failed to decrypt with DPAPI`
> ([yt-dlp #10927](https://github.com/yt-dlp/yt-dlp/issues/10927)). Use the
> `cookies.txt` **`file`** option instead — a "Get cookies.txt" extension reads
> through the browser's own API and isn't affected. Firefox has no such
> restriction, so `from_browser: "firefox"` also works if you prefer a browser
> read.

A dedicated throwaway Google account works fine — any 18+ account will do.
`yt-dlp` also resolves the `PoTokenRequired` case, so this same path fixes
those too.

### When it stops working

**Cookies expire.** Google rotates and revokes sessions server-side well before
a cookie's nominal expiry date, so a scheduled run will eventually need a fresh
export. This is the expected end-of-life of a `cookies.txt`, not a regression —
every failure below degrades to the same clean `transcript_failed` the video
would have had without cookies, and never aborts the run.

Each cause logs a distinct line, so troubleshooting is a lookup rather than a
diagnosis:

| Log line says | Cause | Fix |
|---|---|---|
| `YouTube rejected the configured cookies — they have most likely expired` | Session revoked or rotated | Re-export `cookies.txt` from a signed-in browser |
| `Cookie file '…' not found (looked from …)` | File never landed, was moved, or a relative path resolved against a different working directory | Re-export next to `weekly.bat`, or set an absolute path |
| `yt-dlp can't decrypt this browser's cookies (Chrome/Edge App-Bound Encryption)` | Using `from_browser` on Windows | Switch to the `file` option (see the Windows note above) |
| `yt-dlp is not installed in this environment` | Dependency missing from the venv | `pip install -r requirements.txt` |

Checked in that order, so a decrypt failure that cascades into a "sign in to
confirm" line is reported as the DPAPI problem it is rather than sending you to
re-export a file that was never at fault.

---

## Integrating with other systems (e.g. PerpetualPicks.com)

### Option 0: Pull the ready-made weighted picks feed (easiest)

Every `weekly.bat` run also publishes `docs/picks.json` — the consensus
already distilled into one scored pick per market, ready for a website to
display or an algorithm to weight. No Python needed on the consuming side;
just fetch:

```
https://perpetualpixel.github.io/MMA_Engine/picks.json
```

(No `/docs/` in the path — Pages is configured to serve `main` / `/docs`, so
`docs/` *is* the site root.)

The shape:

```jsonc
{
  "schema_version": 1,
  "generated_at": "2026-08-11T07:28:17+00:00",
  "event": { "name": "UFC 330" },
  "totals": { "picks": 35, "strong": 4, "lean": 23, "pass": 8 },
  "picks": [
    {
      "fight": "Mansour Abdul-Malik vs Dustin Stoltzfus",
      "market": "moneyline",                // or method_of_victory / over_under / round / prop
      "market_label": "Moneyline",
      "selection": "Mansour Abdul-Malik",   // the consensus side
      "consensus_pct": 100.0,               // share of the market's trust weight
      "weight": 14.05,                      // absolute trust weight behind it
      "pick_count": 4,                      // cappers backing it
      "avg_confidence": 6.0,
      "strength": 8.5,                      // 0–10 score (see below)
      "tier": "strong",                     // strong / lean / pass
      "suggested_units": 2.0                // 2u strong, 1u lean, 0 pass
    }
  ]
}
```

`strength` blends how one-sided the market is with how much trust-weight
actually backs it, so one lone capper picking unopposed doesn't score like a
unanimous panel:

```
conviction = consensus_pct / 100
backing    = min(1.0, weight / 20.0)      # 20+ weight ≈ three high-trust cappers
strength   = 10 * conviction * (0.5 + 0.5 * backing)
```

`strength >= 7.5` is tiered "strong" (2 units), `>= 5.0` "lean" (1 unit),
below that "pass" (0 units). Picks are sorted strongest-first. A typical
website integration is: fetch the URL, show every pick with `tier != "pass"`,
and size stakes by `suggested_units` — or feed `strength` into your own
model as one input among many.

#### Getting the new feed on the consuming site *immediately*

GitHub Pages serves `picks.json` through a CDN, and browsers cache it too, so
a site that fetches the plain URL can keep showing the pre-push feed for
minutes after `weekly.bat` finished — which looks exactly like the run having
failed. Two things fix it on the consuming side:

1. **Cache-bust every request** — append a changing query string
   (`picks.json?t=<timestamp>`) and send `cache: 'no-store'`. A unique URL
   misses both the browser cache and the CDN edge cache, so you get the bytes
   that are actually published right now.
2. **Poll and compare `generated_at`** — re-fetch on a short interval and only
   re-render when that timestamp moves. An unchanged feed then costs one small
   request and nothing else.

`integrations/perpetualcode-instant-consensus-refresh.patch` is exactly this,
already written for PerpetualPicks.com. Apply it in the PerpetualCode checkout:

```powershell
git am C:\path\to\MMA_Engine\integrations\perpetualcode-instant-consensus-refresh.patch
node --test test\capper-consensus.test.mjs
```

**On Windows, hand `git am` the file path — never a PowerShell pipe.**
`git show ... | git am` looks equivalent but isn't: Windows PowerShell re-encodes
a native command's output through the console code page, which corrupts the
non-ASCII characters in the patch (em dashes in the comments, mostly). The
hunks whose context lines happen to be pure ASCII still apply, the rest are
rejected with `patch does not apply` — which reads exactly like the patch
being out of date against a drifted checkout, and isn't.

Then push `docs/` (the site) and `npx wrangler deploy` from `worker\` (the
locked daily picks read the same feed, so both sides stay in agreement). After
that, a `weekly.bat` run shows up on an already-open board within a minute,
with no manual refresh.

The feed is rebuilt from `data.json`, so you can also regenerate it by hand:

```powershell
$env:PYTHONPATH = "src"
python -m mma_engine.weighted_picks   # reads docs\data.json, writes docs\picks.json
```

### Option 1: Pull the static `data.json` feed (simple)

`docs/data.json` is the entire output of a run and the thing GitHub Pages
serves — anything that can fetch a URL can read it:

```
https://perpetualpixel.github.io/MMA_Engine/data.json
```

It refreshes every time you run `weekly.bat`, so a puller on a similar or looser
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
              "stated_pick_count": 6,         // backers who voiced a confidence...
              "stated_avg_confidence": 7.8,   // ...and their average, roundup lines excluded
              "cappers": [
                { "id": "artem_mma", "name": "Artem MMA", "confidence": 8,
                  "trust": 7.5, "role": "favorite", "odds": -150, "stake": 2.0,
                  "reasoning": "...", "source": "video",   // or "tracker" (roundup)
                  "video_url": "https://youtu.be/..." }
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

### Option 2: Use the Python `ConsensusClient` (recommended for blending)

If you're writing Python code (e.g., your PerpetualPicks.com algorithm), use the
`ConsensusClient` to fetch and query the data programmatically. **It's a Python library,
not a CLI tool** — you run it inside your Python code, not from the command line.

#### Installation

Add the MMA_Engine repo as a dependency or copy `consensus_client.py` into your project:

```bash
# In your Python project
pip install requests  # if not already installed
```

#### Usage

```python
from mma_engine.consensus_client import ConsensusClient

# Fetch the live consensus
client = ConsensusClient()
data = client.fetch()

# Find a specific fight
fight = client.fight_by_display("Islam Makhachev vs Ian Machado Garry")
if fight:
    # Get the moneyline market
    market = client.market_by_type(fight, "moneyline")
    
    # Get the top consensus pick
    consensus = client.consensus_for_option(market, "Islam Makhachev")
    
    print(f"{consensus['selection']}: {consensus['consensus_pct']}%")
    print(f"  Weight: {consensus['weight']}")
    print(f"  Pick count: {consensus['pick_count']}")
    print(f"  Capper details: {consensus['cappers']}")
```

#### Blend with your algorithm

```python
from mma_engine.consensus_client import ConsensusClient

client = ConsensusClient()

# Your algorithm's prediction
your_pick_pct = 65.0
your_confidence = 7.0

# Get consensus
fight = client.fight_by_display("Islam Makhachev vs Ian Machado Garry")
market = client.market_by_type(fight, "moneyline")
consensus = client.consensus_for_option(market, "Islam Makhachev")

# Blend: 40% consensus, 60% your algorithm
blended = (
    your_pick_pct * your_confidence * 0.6 +
    consensus["consensus_pct"] * (consensus["weight"] / 10.0) * 0.4
) / (your_confidence * 0.6 + (consensus["weight"] / 10.0) * 0.4)

print(f"Blended prediction: {blended:.1f}%")
```

#### API reference

| Method | Returns | Purpose |
|--------|---------|---------|
| `fetch(use_cache=True)` | dict | Fetch the entire consensus payload |
| `fight_by_display(name)` | dict or None | Find a fight by display name (e.g. "Fighter A vs Fighter B") |
| `fight_by_id(fight_id)` | dict or None | Find a fight by its ID |
| `market_by_type(fight, bet_type)` | dict or None | Find a market within a fight (moneyline, method_of_victory, etc.) |
| `consensus_for_option(market, selection)` | dict or None | Find an option and return its full consensus data |
| `all_fights()` | list | Get all fights from the latest consensus |
| `event_info()` | dict | Get event metadata (name, date) |
| `totals()` | dict | Get aggregate counts (fights, picks, cappers, videos) |
| `clear_cache()` | None | Clear cached data so the next fetch hits the network |

See `examples/perpetual_picks_integration.py` for a complete working example.

#### Where does ConsensusClient run?

**In your Python code**, not in PowerShell or as a CLI. It's a library you import and call from within your application. For example:

- If PerpetualPicks.com is a **Python web app** (Flask, Django, FastAPI, etc.), import it in your route or job handler
- If it's a **scheduled task**, call it from your Python script or cron job
- If it's a **batch process**, import it and fetch the consensus at the start

Example: running it from a Python script on your machine:

```bash
python -c "
from mma_engine.consensus_client import ConsensusClient
client = ConsensusClient()
fight = client.fight_by_display('Islam Makhachev vs Ian Machado Garry')
print(fight)
"
```

But normally it's imported inside your algorithm and called as part of your prediction pipeline, not as a standalone tool.

---

If you need push delivery instead of pulling the static file (a webhook, a different schema, authentication), that's a small addition to the `Commit updated data.json` step in `consensus.yml` — happy to wire it up once you know what shape it expects on the other end.

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
| `pasted_picks.enabled` | `true` | Read hand-pasted cards from `pasted/`. With an empty folder this is a no-op. |
| `pasted_picks.dir` | `pasted` | Where those files live. |
| `pasted_picks.max_age_days` | `14` | Skip (and report) files untouched for longer than this. `0` disables the guard. |
| `tracker_picks.enabled` | `true` | Ingest the roundup videos listed in `tracker.picks_videos`. With none listed this is a no-op. |
| `tracker_picks.confidence` | `5` | Confidence every roundup pick enters at — a tally states no conviction. |
| `tracker_picks.max_chunk_chars` | `12000` | Roundup transcripts are name-dense, so they chunk smaller than picks videos. |
| `live_odds.enabled` | `true` | Fetch live moneylines when `ODDS_API_KEY` is set. With no key this is a no-op, so leaving it on is safe. |
| `live_odds.regions` | `us` | Bookmaker regions to median over: `us`, `us2`, `uk`, `eu`, `au` (comma-joined). |
| `proxy.enabled` | `false` | Route YouTube requests through a proxy. Only used for the optional unattended path — see **Optional: fully unattended runs on GitHub Actions**. |
| `proxy.provider` | `webshare` | `webshare` (built-in support) or `generic` (any provider via `MMA_PROXY_URL`). |

---

## Caching and cost

`cache/transcripts/`, `cache/extractions/`, and `cache/tracker_picks/` are
keyed by video ID and gitignored. A re-run after a partial failure re-fetches and re-bills only the
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
  tracker_picks.py           # tracker roundup → every channel's pick
  pasted_picks.py            # hand-pasted cards → picks (paywalled posts)
  transcripts.py             # YouTube fetching, caching, jittered delays
  extract.py                 # Claude structured-output extraction, chunking
  normalize.py               # fighter-name and selection matching
  odds.py                    # live moneylines from The Odds API
  aggregate.py               # trust-weighted consensus math
  weighted_picks.py          # distills data.json into the picks.json feed
  consensus_client.py        # Python client for external integrations
  proxy.py                   # optional proxy for GitHub Actions' cloud IPs
  pipeline.py                # orchestration + CLI
pasted/                      # hand-pasted cards — gitignored, see its README
docs/index.html              # static dashboard (no build step, no CDN)
docs/data.json               # generated output — also the integration feed
docs/picks.json              # weighted picks feed for PerpetualPicks.com
integrations/                # patches for consuming sites (see Option 0)
tests/test_aggregate.py      # normalization + aggregation tests
tests/test_discover.py       # feed parsing, filtering, merge behavior
tests/test_roster.py         # trust arithmetic, pooling, config merge
tests/test_tracker_picks.py  # roundup merging, attribution, config merge
tests/test_pasted_picks.py   # pasted-card naming, staleness, superseding
tests/test_proxy.py          # proxy config from env vars
tests/test_config.py         # settings defaults, merging, env overrides
tests/test_odds.py           # odds parsing, matching, fail-open contract
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
