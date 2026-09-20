# Outlier Watch — automated breakout-video detection

Gets you a Telegram alert the moment any tracked channel posts a video
running well above its own normal pace — no manual checking, no vidIQ
credits, runs on a free schedule via GitHub Actions.

## How the score works

For each channel, every recent video's **views ÷ hours since posted**
(VPH) is compared against the **median VPH of that channel's other recent
videos**. A video scoring 3× or higher is running at 3x that channel's
normal pace right now — that's the alert threshold, adjustable below.

This is the same math the War Room dashboard uses for its Breakout
Detector — this bot just runs it automatically, all the time, instead of
only when you ask for an update.

## Setup (10 minutes, one time — skip steps you already did for the other bot)

### 1. Get a free YouTube Data API key
If you already made one for `war-room.html`, reuse it — same key works
here.
1. Go to [console.cloud.google.com](https://console.cloud.google.com/apis/library/youtube.googleapis.com)
2. Enable "YouTube Data API v3" (skip if already enabled)
3. Go to Credentials → Create Credentials → API key
4. **Application restrictions: set to "None"** (this runs on GitHub's
   servers, not a browser or app, so it can't use referrer/app restrictions)
5. API restrictions: restrict to "YouTube Data API v3" only, for safety

### 2. Telegram bot (skip if you already have one from the other bot)
Same bot and chat ID work here too — see the original `steno-watch-telegram-bot`
README if you need to create one from scratch.

### 3. Create a GitHub repo (or add to your existing one)
Upload these files, keeping the folder structure exactly as-is:
- `channels.json`
- `outlier_watch.py`
- `.github/workflows/outlier_check.yml`

### 4. Add secrets
Repo → Settings → Secrets and variables → Actions → New repository secret:
- `YOUTUBE_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### 5. Turn it on
Actions tab → "Outlier Watch" → Run workflow once manually to test, then
it runs itself every 30 minutes from then on.

## Adding channels yourself

Open `channels.json` and add a new entry:

```json
{"name": "Channel Name Here", "channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx", "is_own": false}
```

To find a channel's ID: go to the channel, right-click → View Page Source,
search for `"channelId"` — or use a free lookup tool like
commentpicker.com/youtube-channel-id.html. Set `"is_own": true` only for
your own channel — this just adds a 🟦 marker in the alert so your own
breakout videos stand out from competitors'.

No code changes needed — the script reads this file fresh every run.

## Tuning the sensitivity

In `.github/workflows/outlier_check.yml`, change `OUTLIER_THRESHOLD`:
- `"2.0"` — more alerts, catches smaller spikes
- `"3.0"` — default, meaningful breakouts only
- `"5.0"` — only the biggest spikes, fewer alerts

## What this does NOT do

- Doesn't replace the War Room dashboard's deeper analysis (Content Radar,
  Gap Detector, Comment Intelligence, Channel DNA) — those still need
  Claude's judgment, not just math. This bot only catches "something
  spiked," not "here's why, and here's what to do about it."
- Won't find adjacent-niche channels on its own — it only watches
  whatever's in `channels.json`. Finding new channels to add is still
  your research, by design.
- Re-alerts on a video only if its score climbs meaningfully higher than
  last time (30%+), so a steadily-growing breakout doesn't spam you every
  30 minutes at the same score.
