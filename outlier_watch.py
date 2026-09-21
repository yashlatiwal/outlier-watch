#!/usr/bin/env python3
"""
Outlier Watch v2  (standard library only, no pip install needed)

Runs every 30 minutes on GitHub Actions. For every channel in channels.json it:
  1. pulls channel stats and the latest 50 uploads from the free YouTube Data API
  2. scores each video: actual views / views this channel's OTHER videos normally
     have at the SAME AGE (same format: Shorts and long videos are kept apart)
  3. sends a Telegram alert for new breakouts, using per-tier thresholds
  4. once an hour writes docs/dashboard_data.json + history.json so the
     dashboard (docs/index.html on GitHub Pages) stays fresh with no manual work
  5. sends one "bot is alive" Telegram message a day, and an error message if
     something breaks (silence from the bot now means the bot is down)

Files it reads : channels.json, tiers.json, history_seed.json (optional, one-time)
Files it writes: outlier_state.json, alerts_log.json, history.json, docs/dashboard_data.json
"""
import html
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))


def path(*parts):
    return os.path.join(ROOT, *parts)


CHANNELS_FILE = path("channels.json")
TIERS_FILE = path("tiers.json")
STATE_FILE = path("outlier_state.json")
ALERTS_FILE = path("alerts_log.json")
HISTORY_FILE = path("history.json")
SEED_FILE = path("history_seed.json")
DASH_FILE = path("docs", "dashboard_data.json")

API_BASE = os.environ.get("YT_API_BASE", "https://www.googleapis.com/youtube/v3")
IST = timezone(timedelta(hours=5, minutes=30))

# ---- tuning knobs (thresholds per tier live in tiers.json) -----------------
DEFAULT_TIERS = {
    "own": {"threshold": 2.0, "min_views": 300, "notify_threshold": 2.0},
    "direct": {"threshold": 2.5, "min_views": 500, "notify_threshold": 2.5},
    "reference": {"threshold": 4.0, "min_views": 5000, "notify_threshold": 8.0},
}
MIN_AGE_HOURS = 3            # ignore videos younger than this (views too noisy)
ALERT_MAX_AGE_DAYS = 45      # only alert on videos newer than this
BASELINE_DAYS = 120          # a channel's "normal" comes from uploads this recent
MIN_BASELINE = 6             # need at least this many comparable videos to score
MIN_FIT_AGE_H = 6            # curve is fitted on videos older than this
DEFAULT_SLOPE = 0.30         # used when a channel's ages are too similar to fit
SLOPE_RANGE = (0.10, 0.90)
REALERT_FACTOR = 1.3         # re-alert a video only if its score grew 30%
SHORT_MAX_SECONDS = 180      # YouTube Shorts can be up to 3 minutes
PUBLISH_EVERY_MIN = int(os.environ.get("PUBLISH_EVERY_MIN", "60"))
HEARTBEAT_HOUR_IST = 9
OWN_SILENCE_WARN_DAYS = 5
DASH_DAYS = 45
DASH_PER_CHANNEL = 15
ERROR_COOLDOWN_H = 6
LOG_CAP = 300
HISTORY_KEEP_DAYS = 400
FATAL_REASONS = {"quotaExceeded", "dailyLimitExceeded", "keyInvalid", "keyExpired",
                 "ipRefererBlocked", "accessNotConfigured"}

UNITS = 0  # rough count of YouTube API quota units used this run


class ApiError(Exception):
    def __init__(self, msg, status=None, reason=""):
        super().__init__(msg)
        self.status = status
        self.reason = reason


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------- file helpers
def load_json(fp, default):
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: could not read {os.path.basename(fp)} ({type(e).__name__}); using default")
        return default


def save_json(fp, data, indent=2, sort_keys=False):
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if indent:
            json.dump(data, f, ensure_ascii=False, indent=indent, sort_keys=sort_keys)
        else:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"), sort_keys=sort_keys)
        f.write("\n")
    os.replace(tmp, fp)


def save_history(history):
    """One line per day so git diffs stay tiny."""
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("{\n")
        days = sorted(history)
        for i, d in enumerate(days):
            comma = "," if i < len(days) - 1 else ""
            f.write(f'{json.dumps(d)}: {json.dumps(history[d], separators=(",", ":"), sort_keys=True)}{comma}\n')
        f.write("}\n")
    os.replace(tmp, HISTORY_FILE)


# ---------------------------------------------------------------------- config
def load_channels():
    raw = load_json(CHANNELS_FILE, None)
    if isinstance(raw, dict):
        raw = raw.get("channels")
    if not isinstance(raw, list) or not raw:
        raise ConfigError("channels.json is missing or empty")

    tiers_cfg = load_json(TIERS_FILE, {}) or {}
    settings = {k: dict(v) for k, v in DEFAULT_TIERS.items()}
    for k, v in (tiers_cfg.get("tier_settings") or {}).items():
        if k in settings and isinstance(v, dict):
            settings[k].update(v)
    overrides = {str(k).strip().lower(): v for k, v in (tiers_cfg.get("channels") or {}).items()}

    out = []
    for e in raw:
        cid = str(e.get("channel_id") or "").strip()
        if not cid:
            continue
        name = str(e.get("name") or cid).strip()
        ov = overrides.get(name.lower())
        ov_tier, extra = None, {}
        if isinstance(ov, str):
            ov_tier = ov
        elif isinstance(ov, dict):
            ov_tier = ov.get("tier")
            extra = {k: v for k, v in ov.items() if k in ("threshold", "min_views", "notify_threshold")}
        tier = e.get("tier") or ov_tier
        if e.get("is_own"):
            tier = "own"
        if tier not in settings:
            tier = "direct"  # unknown channels are treated as direct competitors
        cfg = dict(settings[tier])
        for k in ("threshold", "min_views", "notify_threshold"):
            if k in e:
                cfg[k] = e[k]
        cfg.update(extra)
        cfg["notify_threshold"] = max(cfg["notify_threshold"], cfg["threshold"])
        out.append({"id": cid, "name": name, "tier": tier, "own": tier == "own", **cfg})
    if not out:
        raise ConfigError("channels.json has no usable channel_id entries")
    return out, settings


# ------------------------------------------------------------------ YouTube API
def api_get(endpoint, params):
    global UNITS
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not key:
        raise ConfigError("YOUTUBE_API_KEY secret is missing")
    q = dict(params)
    q["key"] = key
    url = f"{API_BASE}/{endpoint}?{urllib.parse.urlencode(q)}"
    last = None
    for attempt in range(3):
        try:
            UNITS += 1
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            reason, message = "", ""
            try:
                err = json.loads(body)["error"]
                message = err.get("message", "")
                reason = (err.get("errors") or [{}])[0].get("reason", "")
            except Exception:
                pass
            if "API key" in message and "not valid" in message:
                reason = "keyInvalid"
            if e.code in (500, 502, 503, 504) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise ApiError(f"YouTube API error {e.code} on {endpoint} ({reason or 'no reason given'})",
                           e.code, reason)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = type(e).__name__
            time.sleep(2 * (attempt + 1))
    raise ApiError(f"Network problem calling YouTube API on {endpoint} ({last})")


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_duration(s):
    m = re.match(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$", s or "")
    if not m:
        return 0
    d, h, mi, sec = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + sec


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fetch_channel_info(channels):
    info = {}
    for group in chunks([c["id"] for c in channels], 50):
        r = api_get("channels", {"part": "snippet,statistics,contentDetails",
                                 "id": ",".join(group), "maxResults": 50})
        for it in r.get("items", []):
            info[it["id"]] = it
    return info


def fetch_videos(uploads_playlist, now):
    """Latest 50 uploads with stats. Returns newest-first list of dicts."""
    try:
        r = api_get("playlistItems", {"part": "contentDetails", "playlistId": uploads_playlist,
                                      "maxResults": 50})
    except ApiError as e:
        if e.reason == "playlistNotFound":
            return []
        raise
    ids = [it["contentDetails"]["videoId"] for it in r.get("items", [])]
    videos = []
    for group in chunks(ids, 50):
        vr = api_get("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(group),
                                "maxResults": 50})
        for it in vr.get("items", []):
            sn, st, cd = it.get("snippet", {}), it.get("statistics", {}), it.get("contentDetails", {})
            views = to_int(st.get("viewCount"))
            if views is None or sn.get("liveBroadcastContent") in ("live", "upcoming"):
                continue
            pub = parse_time(sn["publishedAt"])
            secs = parse_duration(cd.get("duration"))
            age_h = max((now - pub).total_seconds() / 3600.0, 0.01)
            videos.append({
                "id": it["id"], "title": sn.get("title", "(untitled)"), "pub": pub,
                "age_h": age_h, "views": views, "dur": secs,
                "fmt": "short" if 0 < secs <= SHORT_MAX_SECONDS else "long",
                "vph": views / max(age_h, 1.0), "score": None, "exp": None,
            })
    videos.sort(key=lambda v: v["pub"], reverse=True)
    return videos


# ---------------------------------------------------------------------- scoring
def theil_sen(points):
    slopes = []
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            dx = points[j][0] - points[i][0]
            if abs(dx) >= 0.15:
                slopes.append((points[j][1] - points[i][1]) / dx)
    return statistics.median(slopes) if len(slopes) >= 5 else None


def make_curve(samples):
    """samples: [(age_hours, views)] -> (slope, intercept) of log(views) vs log(age), or None."""
    pts = [(math.log(a), math.log(max(v, 1))) for a, v in samples if a >= MIN_FIT_AGE_H]
    if len(pts) < MIN_BASELINE:
        return None
    s = theil_sen(pts)
    if s is None:
        s = DEFAULT_SLOPE
    s = min(max(s, SLOPE_RANGE[0]), SLOPE_RANGE[1])
    b = statistics.median([y - s * x for x, y in pts])
    return s, b


def expected_views(curve, age_h):
    s, b = curve
    return math.exp(b + s * math.log(max(age_h, MIN_FIT_AGE_H)))


def score_channel_videos(videos):
    """Fills v['score'] and v['exp'] for each video (leave-one-out, same format only)."""
    for v in videos:
        peers = [p for p in videos if p is not v and p["fmt"] == v["fmt"]]
        recent = [p for p in peers if p["age_h"] <= BASELINE_DAYS * 24]
        if len(recent) < MIN_BASELINE:
            recent = peers[:30]
        curve = make_curve([(p["age_h"], p["views"]) for p in recent])
        if curve is None:
            continue
        exp = expected_views(curve, v["age_h"])
        v["exp"] = exp
        v["score"] = v["views"] / max(exp, 1.0)


def is_breakout(v, ch):
    return (v["score"] is not None and v["score"] >= ch["threshold"]
            and v["views"] >= ch["min_views"] and v["age_h"] >= MIN_AGE_HOURS
            and v["age_h"] <= ALERT_MAX_AGE_DAYS * 24)


# --------------------------------------------------------------------- telegram
def telegram_configured():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() and os.environ.get("TELEGRAM_CHAT_ID", "").strip())


def tg_send(text):
    if not telegram_configured():
        return False
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    data = urllib.parse.urlencode({
        "chat_id": os.environ["TELEGRAM_CHAT_ID"].strip(), "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8")).get("ok") is True
    except urllib.error.HTTPError as e:
        print(f"Telegram send failed: HTTP {e.code}")
    except Exception as e:
        print(f"Telegram send failed: {type(e).__name__}")
    return False


def fmt_age(h):
    if h < 48:
        return f"{h:.0f}h"
    return f"{h / 24:.1f} days"


def alert_message(ch, v):
    icon = {"own": "🟦", "direct": "🔥"}.get(ch["tier"], "📡")
    esc = html.escape
    lines = [
        f"{icon} <b>{v['score']:.1f}× breakout</b> - {esc(ch['name'])}",
        f"<a href=\"https://www.youtube.com/watch?v={v['id']}\">{esc(v['title'])}</a>",
        f"{v['views']:,} views, {v['vph']:,.0f} views/hour, posted {fmt_age(v['age_h'])} ago",
    ]
    if v.get("exp"):
        lines.append(f"Normal for this channel at this age: about {v['exp']:,.0f} views")
    return "\n".join(lines)


# ---------------------------------------------------------------- history/deltas
def merge_seed(history, channels):
    """One-time import of history_seed.json ({date: {channel name: stats}})."""
    seed = load_json(SEED_FILE, None)
    if not isinstance(seed, dict):
        return
    by_name = {c["name"].strip().lower(): c["id"] for c in channels}
    for d, per in seed.items():
        if d.startswith("_") or not isinstance(per, dict) or d in history:
            continue
        row = {}
        for name, stats in per.items():
            cid = by_name.get(str(name).strip().lower())
            if cid and isinstance(stats, dict):
                row[cid] = {k: v for k, v in stats.items() if k in ("subs", "views", "videos")}
        if row:
            history[d] = row


def delta_for(history, cid, today, n):
    cur = history.get(today.isoformat(), {}).get(cid)
    if not cur:
        return None
    target = (today - timedelta(days=n)).isoformat()
    dates = sorted(d for d in history if d < today.isoformat() and cid in history[d])
    if not dates:
        return None
    chosen = None
    before = [d for d in dates if d <= target]
    if before and (today - date.fromisoformat(before[-1])).days <= n + max(3, n // 2):
        chosen = before[-1]
    if chosen is None:
        after = [d for d in dates if d > target]
        if after:
            chosen = after[0]
    if chosen is None:
        return None
    days = (today - date.fromisoformat(chosen)).days
    if days < 1:
        return None
    old = history[chosen][cid]
    out = {"days": days}
    for k in ("subs", "views", "videos"):
        if isinstance(cur.get(k), int) and isinstance(old.get(k), int):
            out[k] = cur[k] - old[k]
    return out


def sparkline(history, cid, today, n=30):
    pts = []
    for d in sorted(history):
        if d > today.isoformat():
            continue
        s = history[d].get(cid, {}).get("subs")
        if isinstance(s, int) and (today - date.fromisoformat(d)).days <= n:
            pts.append(s)
    return pts if len(pts) >= 3 else []


# ------------------------------------------------------------------------ state
def load_state():
    """Returns (state, bootstrap). bootstrap=True means no v2 state exists yet."""
    st = load_json(STATE_FILE, {})
    if not isinstance(st, dict) or st.get("version") != 2:
        fresh = {"version": 2, "alerted": {}}
        if isinstance(st, dict) and st.get("last_error_utc"):
            fresh["last_error_utc"] = st["last_error_utc"]
        return fresh, True
    st.setdefault("alerted", {})
    return st, False


def dump_state(st):
    return json.dumps(st, sort_keys=True)


# ------------------------------------------------------------------------- main
def run():
    now = datetime.now(timezone.utc)
    today = now.astimezone(IST).date()
    channels, tier_settings = load_channels()
    state, bootstrap = load_state()
    state_before = dump_state(state)

    alerts_log = load_json(ALERTS_FILE, [])
    if isinstance(alerts_log, dict):
        alerts_log = alerts_log.get("alerts", [])
    if not isinstance(alerts_log, list):
        alerts_log = []
    log_before = json.dumps(alerts_log, sort_keys=True)

    info = fetch_channel_info(channels)
    failed, results = [], []
    for ch in channels:
        item = info.get(ch["id"])
        if not item:
            failed.append({"name": ch["name"], "reason": "channel not found - check the channel_id"})
            continue
        try:
            uploads = item["contentDetails"]["relatedPlaylists"]["uploads"]
            videos = fetch_videos(uploads, now)
        except ApiError as e:
            if e.reason in FATAL_REASONS:
                raise
            failed.append({"name": ch["name"], "reason": str(e)})
            continue
        score_channel_videos(videos)
        results.append((ch, item, videos))

    if not results:
        raise ApiError("No channel could be read - see the run log")

    # ---- alerts -----------------------------------------------------------
    alerted = state["alerted"]
    new_alerts = 0
    boot_marked = 0
    can_notify = telegram_configured()
    for ch, item, videos in results:
        for v in videos:
            if not is_breakout(v, ch):
                continue
            prev = alerted.get(v["id"])
            if prev and v["score"] < prev["score"] * REALERT_FACTOR:
                continue
            entry_state = {"score": round(v["score"], 2), "ts": now.isoformat(timespec="seconds")}
            if bootstrap:
                alerted[v["id"]] = {**entry_state, "notified": False}
                boot_marked += 1
                continue
            notify = can_notify and v["score"] >= ch["notify_threshold"]
            if notify and not tg_send(alert_message(ch, v)):
                continue  # try again next run
            alerted[v["id"]] = {**entry_state, "notified": bool(notify)}
            alerts_log.append({
                "time": now.isoformat(timespec="seconds"), "channel": ch["name"], "channel_id": ch["id"],
                "tier": ch["tier"], "video_id": v["id"], "title": v["title"],
                "score": round(v["score"], 2), "views": v["views"], "vph": round(v["vph"], 1),
                "age_hours": round(v["age_h"], 1), "published_at": v["pub"].isoformat(timespec="seconds"),
                "notified": bool(notify), "url": f"https://www.youtube.com/watch?v={v['id']}",
            })
            new_alerts += 1
    # forget alerts older than 60 days
    cutoff = (now - timedelta(days=60)).isoformat()
    state["alerted"] = {k: v for k, v in alerted.items() if v.get("ts", "") >= cutoff}
    alerts_log = alerts_log[-LOG_CAP:]

    if bootstrap and can_notify:
        tg_send("✅ <b>Outlier Watch v2 is running.</b>\n"
                f"{boot_marked} videos that already look like breakouts were marked as seen, so you are not flooded. "
                "From now on you get alerts only for new breakouts, plus one 'still alive' message a day.")

    # ---- publish dashboard data (hourly, or right after new alerts) --------
    last_pub = state.get("last_publish_utc")
    due = True
    if last_pub:
        mins = (now - parse_time(last_pub)).total_seconds() / 60.0
        due = mins >= PUBLISH_EVERY_MIN - 5
    if os.environ.get("FORCE_PUBLISH") or new_alerts or bootstrap:
        due = True

    own_days = None
    for ch, item, videos in results:
        if ch["own"] and videos:
            own_days = (now - videos[0]["pub"]).total_seconds() / 86400

    if due:
        history = load_json(HISTORY_FILE, {})
        if not isinstance(history, dict):
            history = {}
        merge_seed(history, [c for c, _, _ in results])
        row = {}
        for ch, item, videos in results:
            st = item.get("statistics", {})
            row[ch["id"]] = {
                "subs": None if st.get("hiddenSubscriberCount") else to_int(st.get("subscriberCount")),
                "views": to_int(st.get("viewCount")), "videos": to_int(st.get("videoCount")),
            }
            row[ch["id"]] = {k: v for k, v in row[ch["id"]].items() if v is not None}
        history[today.isoformat()] = row
        keep_from = (today - timedelta(days=HISTORY_KEEP_DAYS)).isoformat()
        history = {d: r for d, r in history.items() if d >= keep_from}
        save_history(history)

        dash = build_dashboard(now, today, results, failed, history, alerts_log, tier_settings)
        save_json(DASH_FILE, dash, indent=None)
        state["last_publish_utc"] = now.isoformat(timespec="seconds")
        print(f"Published dashboard data ({len(dash['videos'])} videos, {len(dash['channels'])} channels)")

    # ---- daily heartbeat ----------------------------------------------------
    now_ist = now.astimezone(IST)
    if (telegram_configured() and now_ist.hour >= HEARTBEAT_HOUR_IST
            and state.get("last_heartbeat_ist_date") != today.isoformat()):
        day_ago = (now - timedelta(hours=24)).isoformat(timespec="seconds")
        n24 = sum(1 for a in alerts_log if str(a.get("time", "")) >= day_ago)
        lines = ["✅ <b>Outlier Watch is running</b>",
                 f"Channels read: {len(results)}/{len(channels)}",
                 f"Breakout alerts in the last 24h: {n24}"]
        if failed:
            lines.append("Problems: " + html.escape(", ".join(f['name'] for f in failed)))
        if own_days is not None:
            warn = " ⚠️ time to post" if own_days >= OWN_SILENCE_WARN_DAYS else ""
            lines.append(f"Your last upload: {own_days:.0f} days ago{warn}")
        lines.append(f"API units used this run: {UNITS} of 10,000 a day (about {UNITS * 48} a day at this pace)")
        if tg_send("\n".join(lines)):
            state["last_heartbeat_ist_date"] = today.isoformat()

    # ---- save what changed ---------------------------------------------------
    if json.dumps(alerts_log, sort_keys=True) != log_before or not os.path.exists(ALERTS_FILE):
        save_json(ALERTS_FILE, alerts_log)
    if dump_state(state) != state_before or not os.path.exists(STATE_FILE):
        save_json(STATE_FILE, state, sort_keys=True)
    print(f"Done. channels ok={len(results)} failed={len(failed)} new_alerts={new_alerts} api_units={UNITS}")


def build_dashboard(now, today, results, failed, history, alerts_log, tier_settings):
    channels_out, videos_out = [], []
    for ch, item, videos in results:
        st = item.get("statistics", {})
        sn = item.get("snippet", {})
        thumb = ((sn.get("thumbnails") or {}).get("default") or {}).get("url", "")
        last = videos[0]["pub"] if videos else None
        channels_out.append({
            "id": ch["id"], "name": ch["name"], "tier": ch["tier"], "own": ch["own"], "thumb": thumb,
            "subs": None if st.get("hiddenSubscriberCount") else to_int(st.get("subscriberCount")),
            "views": to_int(st.get("viewCount")), "videos": to_int(st.get("videoCount")),
            "up7": sum(1 for v in videos if v["age_h"] <= 7 * 24),
            "up30": sum(1 for v in videos if v["age_h"] <= 30 * 24),
            "last_upload": last.isoformat(timespec="seconds") if last else None,
            "d7": delta_for(history, ch["id"], today, 7), "d30": delta_for(history, ch["id"], today, 30),
            "spark": sparkline(history, ch["id"], today),
            "thr": ch["threshold"], "min_views": ch["min_views"],
        })
        recent = [v for v in videos if v["age_h"] <= DASH_DAYS * 24]
        picked = {v["id"]: v for v in recent[:DASH_PER_CHANNEL]}
        for v in recent:
            if is_breakout(v, ch) and len(picked) < DASH_PER_CHANNEL + 10:
                picked[v["id"]] = v
        for v in sorted(picked.values(), key=lambda x: x["pub"], reverse=True):
            videos_out.append({
                "id": v["id"], "cid": ch["id"], "t": v["title"][:140],
                "pub": v["pub"].isoformat(timespec="seconds"), "age_h": round(v["age_h"], 1),
                "views": v["views"], "vph": round(v["vph"], 1),
                "score": None if v["score"] is None else round(v["score"], 2),
                "exp": None if v["exp"] is None else round(v["exp"]),
                "fmt": v["fmt"], "brk": bool(is_breakout(v, ch)),
            })
    return {
        "version": 2, "generated_at": now.isoformat(timespec="seconds"),
        "run": {"channels_total": len(results) + len(failed), "channels_ok": len(results),
                "failed": failed, "api_units": UNITS},
        "tiers": tier_settings, "channels": channels_out, "videos": videos_out,
        "alerts": alerts_log[-40:][::-1],
    }


def report_error(e):
    msg = str(e) if isinstance(e, (ApiError, ConfigError)) else f"{type(e).__name__}: {e}"
    print(f"ERROR: {msg}")
    try:
        state, boot = load_state()
        last = state.get("last_error_utc")
        now = datetime.now(timezone.utc)
        if not last or (now - parse_time(last)).total_seconds() > ERROR_COOLDOWN_H * 3600:
            if tg_send(f"⚠️ <b>Outlier Watch problem</b>\n{html.escape(msg)}\n"
                       "Check the Actions tab on GitHub. This message repeats at most every 6 hours."):
                state["last_error_utc"] = now.isoformat(timespec="seconds")
                if boot:
                    # keep the file "not v2" so the first good run still bootstraps quietly
                    state = {"version": 1, "last_error_utc": state["last_error_utc"]}
                save_json(STATE_FILE, state, sort_keys=True)
    except Exception as inner:
        print(f"Could not report error: {type(inner).__name__}")


def main():
    try:
        run()
    except Exception as e:  # noqa: BLE001 - top-level guard so the bot always reports
        report_error(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
