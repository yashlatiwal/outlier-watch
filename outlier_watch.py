#!/usr/bin/env python3
"""
Outlier Watch — automated breakout-video detection.

Checks every channel in channels.json, computes an outlier score for each
recent video (views/hour ÷ that channel's own median views/hour), and
sends a Telegram alert the moment a video crosses the threshold.

Uses the free YouTube Data API v3 — NOT vidIQ — so it has no credit limits
and can run as often as you like within YouTube's free daily quota
(10,000 units/day; this script costs roughly 3 units per channel per run,
so checking 6 channels every 30 minutes costs ~864 units/day — comfortably
inside the free tier even checking every 10 minutes).

Add or remove channels by editing channels.json — no code changes needed.
"""

import json
import os
import urllib.request
import urllib.parse

API_KEY = os.environ.get("YOUTUBE_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Alert when a video's VPH is this many times its channel's own median VPH
OUTLIER_THRESHOLD = float(os.environ.get("OUTLIER_THRESHOLD", "3.0"))

# Only look at videos published within this many hours (avoids re-scoring old catalog)
MAX_VIDEO_AGE_HOURS = 24 * 45  # 45 days

CHANNELS_FILE = "channels.json"
STATE_FILE = "outlier_state.json"

BASE = "https://www.googleapis.com/youtube/v3/"


def yt_get(path, params):
    params = dict(params)
    params["key"] = API_KEY
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def load_channels():
    with open(CHANNELS_FILE, "r") as f:
        return json.load(f)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


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
    import datetime
    dt = datetime.datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ")
    dt = dt.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - dt).total_seconds() / 3600


def get_channel_videos(channel_id, max_results=50):
    """Return recent videos with view counts and computed VPH."""
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
    # videos.list accepts at most 50 ids per call — batch if max_results ever exceeds 50
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


def main():
    channels = load_channels()
    state = load_state()
    new_alerts = 0

    for ch in channels:
        name = ch["name"]
        channel_id = ch["channel_id"]
        try:
            videos = get_channel_videos(channel_id)
        except Exception as e:
            print(f"Failed to fetch {name}: {e}")
            continue

        # only score videos published within the age window
        recent = [v for v in videos if v["age_hours"] <= MAX_VIDEO_AGE_HOURS]
        if len(recent) < 2:
            continue

        for video in recent:
            others_vph = [v["vph"] for v in recent if v["id"] != video["id"]]
            baseline = median(others_vph) if others_vph else video["vph"]
            score = video["vph"] / baseline if baseline > 0 else 1.0
            video["score"] = round(score, 2)

        # find outliers above threshold, not already alerted at this score tier
        for video in recent:
            if video["score"] < OUTLIER_THRESHOLD:
                continue
            vid = video["id"]
            prev_score = state.get(vid, {}).get("last_alerted_score", 0)
            # re-alert if score has grown meaningfully since last alert (catches videos still climbing)
            if prev_score and video["score"] < prev_score * 1.3:
                continue

            own_tag = "🟦 YOUR VIDEO" if ch.get("is_own") else ""
            message = (
                f"🚨 <b>Outlier detected</b> {own_tag}\n\n"
                f"<b>{name}</b>\n"
                f"{video['title']}\n\n"
                f"Score: <b>{video['score']}×</b> channel average\n"
                f"Views: {video['views']:,} · Age: {video['age_hours']:.1f}h\n"
                f"https://www.youtube.com/watch?v={vid}"
            )
            send_telegram(message)
            new_alerts += 1
            state[vid] = {"last_alerted_score": video["score"], "channel": name, "title": video["title"]}

    save_state(state)
    print(f"Done. {new_alerts} alert(s) sent this run.")


if __name__ == "__main__":
    main()
