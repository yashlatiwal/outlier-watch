#!/usr/bin/env python3
"""
SSC news watcher (standard library only).

Every ~15 minutes it reads Google News headlines about SSC Stenographer and sends
you ONE Telegram message the first time each big event (answer key, result, cutoff,
skill test, admit card, exam date or notice) is reported as released. Copies of the
same story from other sites are counted but do not ping you again.

Reads : watch.json (settings, optional), ssc_state.json
Writes: ssc_state.json
Secrets used: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (same ones the main bot uses)
"""
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(ROOT, "watch.json")
STATE_FILE = os.path.join(ROOT, "ssc_state.json")
FEED_BASE = os.environ.get("NEWS_FEED_BASE", "https://news.google.com/rss/search")
IST = timezone(timedelta(hours=5, minutes=30))
ERROR_COOLDOWN_H = 6
SEEN_KEEP_DAYS = 7
HEARTBEAT_EVERY_DAYS = 7

DEFAULT_CONFIG = {
    "queries": [
        'SSC Stenographer (answer key OR result OR cutoff OR "skill test" OR "admit card" OR notice) when:2d',
        "SSC Steno 2026 (out OR released OR declared) when:2d",
    ],
    "must_include": ["steno"],
    "ignore": ["ldce", "limited departmental", "departmental competitive"],
    "events": {
        "Answer key": ["answer key", "ans key", "response sheet", "आंसर की", "आन्सर की"],
        "Result": ["result", "scorecard", "score card", "रिजल्ट", "परिणाम"],
        "Cutoff": ["cut off", "cutoff", "कटऑफ"],
        "Skill test": ["skill test", "typing test", "स्किल टेस्ट"],
        "Admit card": ["admit card", "city slip", "hall ticket", "एडमिट कार्ड"],
        "Exam date or notice": ["exam date", "revised date", "notice", "schedule", "notification"],
    },
    "released_words": ["out", "released", "release", "declared", "announced", "uploaded", "activated",
                       "live", "available", "issued", "published", "जारी", "घोषित"],
    "not_yet_words": ["soon", "expected", "likely", "when", "how to", "kab", "prediction", "steps to",
                      "guide", "कब", "संभावित"],
    "max_age_hours": 6,
    "cooldown_hours": 36,
}


def load_json(fp, default):
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        print(f"WARNING: could not read {os.path.basename(fp)}; using default")
        return default


def save_json(fp, data):
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, fp)


def norm(s):
    return re.sub(r"[^0-9a-z\u0900-\u097f]+", " ", str(s).lower()).strip()


def padded(s):
    return " " + norm(s) + " "


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    user = load_json(CONFIG_FILE, {}) or {}
    for k in DEFAULT_CONFIG:
        if k in user and user[k]:
            cfg[k] = user[k]
    return cfg


# ------------------------------------------------------------------- feed reading
def fetch_feed(query):
    params = {"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
    url = FEED_BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (outlier-watch)"})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code < 500 and e.code != 429:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = type(e).__name__
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Could not read the news feed ({last})")


def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src = it.find("source")
        source = (src.text or "").strip() if src is not None else ""
        if source and title.endswith(" - " + source):
            title = title[: -(len(source) + 3)].strip()
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate")).astimezone(timezone.utc)
        except Exception:
            pub = None
        if title:
            items.append({"title": title, "link": (it.findtext("link") or "").strip(), "source": source, "pub": pub})
    return items


# ------------------------------------------------------------------ classification
def any_word(text_padded, words):
    return any(" " + norm(w) + " " in text_padded for w in words if norm(w))


def classify(item, cfg):
    """Returns (event_name, confirmed) or None if the headline is not about a tracked event."""
    t = padded(item["title"])
    if not any(norm(m) in t for m in cfg["must_include"]):
        return None
    if any(norm(x) in t for x in cfg["ignore"]):
        return None
    event = None
    for name, words in cfg["events"].items():
        if any_word(t, words):
            event = name
            break
    if event is None:
        return None
    confirmed = any_word(t, cfg["released_words"]) and not any_word(t, cfg["not_yet_words"])
    return event, confirmed


def item_key(item):
    return hashlib.sha1(norm(item["title"]).encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------- telegram
def telegram_configured():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() and os.environ.get("TELEGRAM_CHAT_ID", "").strip())


def tg_send(text):
    if not telegram_configured():
        print("Telegram is not configured; message not sent")
        return False
    data = urllib.parse.urlencode({
        "chat_id": os.environ["TELEGRAM_CHAT_ID"].strip(), "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN'].strip()}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8")).get("ok") is True
    except urllib.error.HTTPError as e:
        print(f"Telegram send failed: HTTP {e.code}")
    except Exception as e:
        print(f"Telegram send failed: {type(e).__name__}")
    return False


def ago(pub, now):
    if pub is None:
        return "just now"
    mins = max(int((now - pub).total_seconds() / 60), 0)
    return f"{mins} min ago" if mins < 90 else f"{mins // 60} h ago"


# --------------------------------------------------------------------------- main
def run():
    now = datetime.now(timezone.utc)
    cfg = load_config()
    state = load_json(STATE_FILE, None)
    bootstrap = not isinstance(state, dict) or state.get("version") != 1
    if bootstrap:
        state = {"version": 1, "seen": {}, "events": {}}
    before = json.dumps(state, sort_keys=True)

    items, failures = {}, 0
    for q in cfg["queries"]:
        try:
            for it in parse_feed(fetch_feed(q)):
                items.setdefault(item_key(it), it)
        except (RuntimeError, ET.ParseError) as e:
            failures += 1
            print(f"Query failed: {e}")
    if failures == len(cfg["queries"]):
        raise RuntimeError("Every news query failed - check the run log")

    alerts = 0
    for key, it in sorted(items.items(), key=lambda kv: kv[1]["pub"] or now):
        if key in state["seen"]:
            continue
        state["seen"][key] = now.isoformat(timespec="seconds")
        if bootstrap:
            continue
        hit = classify(it, cfg)
        if not hit or not hit[1]:
            continue
        event = hit[0]
        if it["pub"] is not None and (now - it["pub"]).total_seconds() > cfg["max_age_hours"] * 3600:
            continue
        prev = state["events"].get(event)
        if prev and (now - datetime.fromisoformat(prev["ts"])).total_seconds() < cfg["cooldown_hours"] * 3600:
            prev["count"] = prev.get("count", 1) + 1
            continue
        msg = (f"🚨 <b>SSC Steno: {html.escape(event)}</b>\n"
               f"<a href=\"{html.escape(it['link'], quote=True)}\">{html.escape(it['title'])}</a>\n"
               f"{html.escape(it['source'] or 'News')}, {ago(it['pub'], now)}\n"
               "First report of this event. Competitors are about to post: this is your window.")
        if tg_send(msg):
            state["events"][event] = {"ts": now.isoformat(timespec="seconds"), "title": it["title"],
                                      "link": it["link"], "source": it["source"], "count": 1}
            alerts += 1

    cutoff = (now - timedelta(days=SEEN_KEEP_DAYS)).isoformat(timespec="seconds")
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}

    if bootstrap and telegram_configured():
        tg_send("✅ <b>SSC watcher is running.</b>\nI read the current headlines and marked them as seen. "
                "From now on you get one message the first time an answer key, result, cutoff, skill test, "
                "admit card or exam date is reported as released.")

    today = now.astimezone(IST).date()
    last_hb = state.get("last_heartbeat")
    if (not bootstrap and telegram_configured() and
            (not last_hb or (today - datetime.fromisoformat(last_hb).date()).days >= HEARTBEAT_EVERY_DAYS)):
        if tg_send(f"✅ SSC watcher still running. Headlines in view: {len(items)}. "
                   f"Event types alerted so far: {len(state['events'])}."):
            state["last_heartbeat"] = today.isoformat()
    if bootstrap:
        state["last_heartbeat"] = today.isoformat()

    if json.dumps(state, sort_keys=True) != before or not os.path.exists(STATE_FILE):
        save_json(STATE_FILE, state)
    print(f"Done. headlines={len(items)} new_alerts={alerts} bootstrap={bootstrap} failed_queries={failures}")


def report_error(e):
    msg = f"{type(e).__name__}: {e}"
    print(f"ERROR: {msg}")
    try:
        state = load_json(STATE_FILE, None)
        if not isinstance(state, dict):
            state = {"version": 0}
        last = state.get("last_error")
        now = datetime.now(timezone.utc)
        if not last or (now - datetime.fromisoformat(last)).total_seconds() > ERROR_COOLDOWN_H * 3600:
            if tg_send(f"⚠️ <b>SSC watcher problem</b>\n{html.escape(msg)}\nThis repeats at most every 6 hours."):
                state["last_error"] = now.isoformat(timespec="seconds")
                save_json(STATE_FILE, state)
    except Exception as inner:
        print(f"Could not report error: {type(inner).__name__}")


def main():
    try:
        run()
    except Exception as e:  # noqa: BLE001
        report_error(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
