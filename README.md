# Outlier Watch v2 - automated breakout detection and War Room dashboard

Watches a list of YouTube channels around the clock, tells you on Telegram when
one of them has a video running far above that channel's own normal, and keeps
a live dashboard up to date. It runs free on GitHub Actions using the free
YouTube Data API. No vidIQ credits, no server, no manual refresh.

This file is written for anyone (human or AI agent) picking the project up
without context. Read it fully before changing anything.

## What runs and when

Every 30 minutes GitHub Actions runs `outlier_watch.py` (standard library only,
nothing to install). It:

1. Reads `channels.json` (who to watch) and `tiers.json` (how loud each one is).
2. Pulls channel stats and the latest 50 uploads per channel (about 27 API
   units per run, about 1,300 a day against the free 10,000).
3. Scores every video (see "How the score works").
4. Sends a Telegram alert for a new breakout, and logs it in `alerts_log.json`.
5. About once an hour (or right after a new alert) writes `history.json` and
   `docs/dashboard_data.json`. The dashboard reads that file.
6. Sends one "still running" Telegram message a day (after 9:00 IST) that also
   says how many days since the owner's last upload. If that message stops
   arriving, the bot is down. It also sends an error message (at most once
   every 6 hours) if the API key or quota breaks.

## Repo structure

```
outlier-watch/
├── channels.json                        <- who to watch (name, channel_id, is_own)
├── tiers.json                           <- tier and alert thresholds per channel
├── outlier_watch.py                     <- the bot
├── history_seed.json                    <- optional one-time import, safe to delete
├── outlier_state.json                   <- auto-generated, do not edit
├── alerts_log.json                      <- auto-generated log of every alert
├── history.json                         <- auto-generated daily subscriber/view snapshots
├── docs/
│   ├── index.html                       <- the dashboard (GitHub Pages)
│   ├── notes.json                       <- hand-written analysis shown on the dashboard
│   └── dashboard_data.json              <- auto-generated, the dashboard reads this
├── README.md
└── .github/workflows/outlier_check.yml  <- the schedule
```

## How the score works

Old version: views per hour divided by the channel's median. That favoured
brand-new videos, because views per hour is always high in the first hours.

Now: for each video the bot fits a simple curve of "views versus age" from that
channel's OTHER recent videos (same format only, Shorts and long videos are
kept apart, and the video being scored is left out of its own baseline). The
score is actual views divided by the views that curve predicts at the video's
age. 1x is normal, 3x is three times normal for that age. A channel needs at
least 6 comparable videos to get a score, otherwise the video shows "no score".

Alert rules, per tier (set in `tiers.json`):

| Tier | Meaning | Logged at | Minimum views | Telegram at |
|---|---|---|---|---|
| own | the owner's channel | 2.0x | 300 | 2.0x |
| direct | same niche | 2.5x | 500 | 2.5x |
| reference | big or general channels | 4.0x | 5,000 | 8.0x |

A video younger than 3 hours is ignored. A video is re-alerted only if its
score has grown 30% since the last alert. A single channel can override any of
the three numbers in `tiers.json` (see QEng and Gagan there).

Important limit: a score compares a video with its own channel. During an exam
week every video on a channel surges together, so a huge views-per-hour number
can still score about 1x. That is correct: it means the topic is hot, not that
one video broke out.

## Setup (for anyone standing this up fresh)

1. **YouTube Data API key** - Google Cloud Console, enable "YouTube Data API
   v3", Credentials, Create API key. Application restrictions must be "None"
   (this runs on GitHub's servers). API restrictions can be locked to YouTube
   Data API v3 only.
2. **Telegram bot** - message @BotFather, `/newbot`, save the token. Message
   @userinfobot for your numeric chat ID. Send `/start` to your new bot.
3. **GitHub secrets** (Settings, Secrets and variables, Actions):
   `YOUTUBE_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
4. **Turn on the dashboard** - Settings, Pages, "Deploy from a branch", branch
   `main`, folder `/docs`. The dashboard address is then
   `https://<user>.github.io/<repo>/`.
5. Run the workflow once by hand (Actions tab, Run workflow). A manual run
   always refreshes the dashboard data immediately.

The first run after upgrading from v1 is a quiet "bootstrap": videos that
already look like breakouts are marked as seen so you are not flooded, and you
get one Telegram message saying v2 is running.

## Adding, removing or re-tiering channels

- Add a channel: add a line to `channels.json`
  (`{"name": "...", "channel_id": "UC...", "is_own": false}`), then add the same
  name to `tiers.json`. A channel missing from `tiers.json` is treated as
  direct.
- Change how loud a channel is: change its tier in `tiers.json`. Nothing else.
- Find a channel ID: channel page, View Page Source, search `"channelId"`.

## What is automatic and what is not

Automatic: subscriber counts, views, uploads, last-upload date, 30-day changes
(built from the daily snapshots in `history.json`), scores, the live feed,
alerts, the health banner.

Not automatic: `docs/notes.json`, the hand-written analysis. It shows its own
age on the dashboard. Refresh it by giving an AI assistant the current
`dashboard_data.json` and asking for updated notes.

Limits of the free API: it cannot give search volume or keyword data, and
subscriber counts above 1,000 are rounded to 3 significant digits (so a small
channel's growth rate is approximate).

## Known issues and lessons learned (read before debugging from scratch)

- **GitHub's "Update secret" box always shows empty**, even when the secret is
  saved. That is normal.
- **Copy-pasting long code into GitHub's web editor can silently truncate it.**
  If something that should happen does not, check that the file in the repo
  ends exactly like the source (`outlier_watch.py` ends with
  `if __name__ == "__main__": main()`).
- **Retyped tokens fail in small ways** - trailing spaces, lowercase `l` versus
  uppercase `L`. Copy and paste them.
- **`.github` needs the literal leading dot**, or Actions will not see the
  workflow.
- **The workflow's "Save data files" step must list every output file.** A new
  output file does nothing until it is added to that list in
  `outlier_check.yml`, otherwise it is created on the runner and thrown away.
- **Silent failure has happened before** (a truncated script ran without
  errors and wrote nothing). That is why the daily "still running" message and
  the dashboard's red "Bot has not updated" banner exist. Trust their absence.
- **GitHub can delay scheduled runs** by many minutes at busy times. The
  dashboard only complains after 4 hours without new data.
- **The repo is public** so the raw files can be read without authentication.
  Secrets stay private, but the channel list and alert history are visible to
  anyone. Keep that in mind before adding anything sensitive to `channels.json`.
