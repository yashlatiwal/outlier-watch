#!/usr/bin/env python3
"""
Outlier Watch — automated breakout-video detection.

Checks every channel in channels.json, computes an outlier score for each
recent video (views/hour ÷ that channel's own median views/hour), and
sends a Telegram alert the moment a video crosses the threshold.

Uses the free YouTube Data API v3 — NOT vidIQ — so it has no credit limits
and can run as often as you like within YouTube's free daily quota.

Maintains TWO state files:
  - outlier_state.json — dedup memory (which videos have been alerted on,
    at what score) so the same breakout doesn't spam you every 30 minutes.
  - alerts_log.json — a permanent, append-only log of every alert ever
    sent, capped at the last 200 entries. This is what lets the War Room
    dashboard (or anything else) see "what has the bot caught since I last
    checked", not just "what's happening right now."

Add or remove channels by editing channels.json — no code changes needed.
"""

import json
import os
import datetime
import urllib.request
import urllib.parse

API_KEY = os.environ.get("YOUTUBE_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

OUTLIER_THRESHOLD = float(os.environ.get("OUTLIER_THRESHOLD", "3.0"))
MAX_VIDEO_AGE_HOURS = 24 * 45  # 45 days
MAX_LOG_ENTRIES = 200

CHANNELS_FILE = "channels.json"
STATE_FILE = "outlier_state.json"
LOG_FILE = "alerts_log.json"

BASE = "https://www.googleapis.com/youtube/v3/"


def yt_get(path, params):
    params = dict(params)
    params["key"] = API_KEY
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Missing Telegram credentials — printing instead of sending.")
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps(
        {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except Exception as e:
        print(f"Telegram send failed: {e}")


def iso_to_epoch_hours_ago(published_at):
    dt = datetime.datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ")
    dt = dt.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - dt).total_seconds() / 3600


def get_channel_videos(channel_id, max_results=50):
    ch = yt_get("channels", {"part": "contentDetails", "id": channel_id})
    items = ch.get("items", [])
    if not items:
        return []
    uploads_playlist = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    pl = yt_get(
        "playlistItems",
        {"part": "contentDetails", "playlistId": uploads_playlist, "maxResults": max_results},
    )
    video_ids = [it["contentDetails"]["videoId"] for it in pl.get("items", [])]
    if not video_ids:
        return []

    videos = []
    for i in range(0, len(video_ids), 50):
        batch_ids = video_ids[i:i + 50]
        vids = yt_get(
            "videos",
            {"part": "snippet,statistics", "id": ",".join(batch_ids)},
        )
        for v in vids.get("items", []):
            published_at = v["snippet"]["publishedAt"]
            age_hours = max(iso_to_epoch_hours_ago(published_at), 0.1)
            views = int(v["statistics"].get("viewCount", 0))
            vph = views / age_hours
            videos.append(
                {
                    "id": v["id"],
                    "title": v["snippet"]["title"],
                    "published_at": published_at,
                    "age_hours": age_hours,
                    "views": views,
                    "vph": vph,
                }
            )
    return videos


def median(values):
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def append_log(log, entry):
    log.append(entry)
    if len(log) > MAX_LOG_ENTRIES:
        log[:] = log[-MAX_LOG_ENTRIES:]


def main():
    channels = load_json(CHANNELS_FILE, [])
    state = load_json(STATE_FILE, {})
    log = load_json(LOG_FILE, [])
    new_alerts = 0
    run_time = datetime.datetime.now(datetime.timezone.utc).isoformat()

    for ch in channels:
        name = ch["name"]
        channel_id = ch["channel_id"]
        try:
            videos = get_channel_videos(channel_id)
        except Exception as e:
            print(f"Failed to fetch {name}: {e}")
            continue

        recent = [v for v in videos if v["age_hours"] <= MAX_VIDEO_AGE_HOURS]
        if len(recent) < 2:
            continue

        for video in recent:
            others_vph = [v["vph"] for v in recent if v["id"] != video["id"]]
