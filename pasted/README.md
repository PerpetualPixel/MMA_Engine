# Pasted picks

Drop a plain-text file in this folder to give a capper's picks to the engine
by hand — for cards that never appear on YouTube because the capper now posts
them to Patreon (or a Discord, or a newsletter) and leaves a two-play teaser
on the channel.

**Name the file after the capper.** Any of these work, matched against
`config.json`:

```
pasted/funky_picks.txt          # the capper id
pasted/Funky Picks.txt          # their name
pasted/Funk Picks.txt           # any alias listed on them
```

**Paste the text.** Whatever the capper wrote — their card, their write-up,
their unit sizes. The same extractor that reads a transcript reads this, so
picks arrive with real confidence, stated odds, and their own reasoning, and
count as fully as a pick from a video.

An optional first line naming the source is kept as the pick's link:

```
https://www.patreon.com/posts/12345678

Main event: Hernandez by decision, 2 units. I think the wrestling...
```

## Rules of the road

- **Only paste what you're entitled to read.** This folder is gitignored —
  its contents are someone else's paid writing and never get published by
  this repo. Nothing here fetches from Patreon; a person puts the text in.
- **A pasted card beats that capper's video.** For any fight the paste
  covers, their teaser video's pick for the same fight is dropped — the paste
  is the full card.
- **Files go stale.** Anything untouched for longer than
  `settings.pasted_picks.max_age_days` (14 by default) is skipped and
  reported, so last month's card can't quietly keep voting. Update the file
  each week, or delete it.
- One file per capper. `.txt` and `.md` are read; this README is ignored.

One-off without using the folder:

```bash
PYTHONPATH=src python -m mma_engine --picks-from-text funky_picks=/path/to/card.txt
```
