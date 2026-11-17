"""
NSE Filings -> Telegram channel bot
-----------------------------------
Checks NSE's official RSS feeds and posts NEW items to a Telegram channel:
  - Financial Results
  - Order wins (picked out of the Announcements feed)
  - Board Meetings
  - Corporate Actions (dividend, bonus, split, buyback...)

You normally only need to edit the SETTINGS section below.
"""

import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# ============================ SETTINGS ============================

# Turn a category off by changing True to False.
FEEDS = {
    "results": {
        "on": True,
        "label": "📊 RESULTS",
        "url": "https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml",
    },
    "orders": {
        "on": True,
        "label": "📦 ORDER WIN",
        "url": "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml",
    },
    "board": {
        "on": True,
        "label": "🗓️ BOARD MEETING",
        "url": "https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml",
    },
    "actions": {
        "on": True,
        "label": "💰 CORPORATE ACTION",
        "url": "https://nsearchives.nseindia.com/content/RSS/Corporate_action.xml",
    },
}

# An announcement counts as an "order win" if it contains one of these words...
ORDER_WORDS = [
    "order", "contract", "letter of award", "letter of intent", "loa", "loi",
    "work order", "purchase order", "bagging", "awarded", "l1 bidder",
]
# ...and none of these (so court orders, tax orders etc. are skipped).
ORDER_SKIP_WORDS = [
    "court", "tribunal", "nclt", "nclat", "sebi", "penalty", "gst", "tax",
    "income tax", "demand", "show cause", "arbitration", "high court",
    "supreme court", "in order to", "order of the", "interim order",
    "final order", "adjudication", "rbi",
]

FOOTER = "\n\n<i>Source: NSE filings. For information only, not investment advice.</i>"
SEEN_FILE = "seen.json"
MAX_POSTS_PER_RUN = 40      # safety limit so the channel is never flooded
SECONDS_BETWEEN_POSTS = 3   # Telegram allows ~20 posts/minute in a channel

# ==================================================================

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "").strip()  # e.g. @mychannel
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"            # print instead of posting

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def clean(text):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_items(xml_text):
    """Return a list of dicts: title, link, desc, date."""
    items = []
    try:
        root = ET.fromstring(xml_text.strip())
        for it in root.iter("item"):
            items.append({
                "title": clean(it.findtext("title")),
                "link": clean(it.findtext("link")),
                "desc": clean(it.findtext("description")),
                "date": clean(it.findtext("pubDate")),
            })
    except ET.ParseError:
        # NSE feeds are sometimes slightly malformed; fall back to a simple scan.
        for block in re.findall(r"<item>(.*?)</item>", xml_text, flags=re.S | re.I):
            def tag(name):
                m = re.search(rf"<{name}>(.*?)</{name}>", block, flags=re.S | re.I)
                if not m:
                    return ""
                v = re.sub(r"^<!\[CDATA\[|\]\]>$", "", m.group(1).strip())
                return clean(v)
            items.append({"title": tag("title"), "link": tag("link"),
                          "desc": tag("description"), "date": tag("pubDate")})
    return [i for i in items if i["title"]]


def is_order_win(item):
    text = f" {item['title']} {item['desc']} ".lower()
    has_word = any(re.search(rf"\b{re.escape(w)}", text) for w in ORDER_WORDS)
    has_skip = any(w in text for w in ORDER_SKIP_WORDS)
    return has_word and not has_skip


def pretty_results(desc):
    """Turn NSE's 'RELATING TO:First Quarter |AUDITED/UNAUDITED:Unaudited |...' into one short line."""
    parts = {}
    for chunk in desc.split("|"):
        if ":" in chunk:
            k, v = chunk.split(":", 1)
            parts[k.strip().upper()] = v.strip()
    period = parts.get("RELATING TO", "")
    audit = parts.get("AUDITED/UNAUDITED", "")
    cons = parts.get("CONSOLIDATED/NON-CONSOLIDATED", "")
    cons = {"Non-Consolidated": "Standalone"}.get(cons, cons)
    ended = parts.get("PERIOD ENDED", "")
    bits = [b for b in [period, audit, cons] if b]
    line = " · ".join(bits)
    if ended:
        line += f"\nPeriod ended: {ended}"
    return line or desc


def item_key(feed_name, item):
    # Results: one post per company + period (skip the separate standalone/consolidated copy).
    if feed_name == "results":
        m = re.search(r"PERIOD ENDED:\s*([^|]+)", item["desc"], flags=re.I)
        basis = f"{item['title']}|{m.group(1).strip() if m else item['desc']}"
    else:
        basis = f"{item['title']}|{item['desc']}|{item['date']}"
    return hashlib.sha1(f"{feed_name}|{basis}".encode()).hexdigest()[:16]


def build_message(feed_name, item):
    label = FEEDS[feed_name]["label"]
    body = pretty_results(item["desc"]) if feed_name == "results" else item["desc"]
    if len(body) > 700:
        body = body[:700].rsplit(" ", 1)[0] + "…"
    msg = f"<b>{label}</b>\n\n<b>{html.escape(item['title'])}</b>\n{html.escape(body)}"
    if item["date"]:
        msg += f"\n\n🕒 {html.escape(item['date'])}"
    if item["link"].startswith("http"):
        msg += f'\n🔗 <a href="{html.escape(item["link"], quote=True)}">View filing on NSE</a>'
    return msg + FOOTER


def send(text):
    if DRY_RUN:
        print("---- WOULD POST ----\n" + text + "\n")
        return True
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHANNEL, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
                return json.loads(r.read()).get("ok", False)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if e.code == 429:  # Telegram says slow down
                wait = json.loads(detail).get("parameters", {}).get("retry_after", 10)
                time.sleep(int(wait) + 1)
                continue
            print(f"Telegram error {e.code}: {detail}")
            return False
        except Exception as e:
            print(f"Telegram send failed: {e}")
            time.sleep(5)
    return False


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return None
    with open(SEEN_FILE) as f:
        return json.load(f)


def save_seen(keys):
    with open(SEEN_FILE, "w") as f:
        json.dump(keys[-8000:], f)


def main():
    if not DRY_RUN and (not BOT_TOKEN or not CHANNEL):
        sys.exit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHANNEL secret.")

    seen_list = load_seen()
    first_run = seen_list is None
    seen_list = seen_list or []
    seen = set(seen_list)

    new_posts = []
    for name, feed in FEEDS.items():
        if not feed["on"]:
            continue
        try:
            items = parse_items(fetch(feed["url"]))
            print(f"{name}: {len(items)} items in feed")
        except Exception as e:
            print(f"Could not read {name} feed: {e}")
            continue
        if name == "orders":
            items = [i for i in items if is_order_win(i)]
            print(f"  ...{len(items)} look like order wins")
        for item in reversed(items):  # oldest first
            k = item_key(name, item)
            if k in seen:
                continue
            seen.add(k)
            new_posts.append((k, name, item))

    if first_run:
        seen_list += [k for k, _, _ in new_posts]
        # Don't dump the whole backlog on day one: just remember it.
        save_seen(seen_list)
        send("✅ <b>Filings bot connected.</b>\nNew NSE results, order wins, board meetings "
             "and corporate actions will appear here automatically." + FOOTER)
        print(f"First run: remembered {len(seen_list)} existing items, posted nothing else.")
        return

    # Anything beyond the per-run limit (or a failed post) is left for the next run.
    posted = 0
    for k, name, item in new_posts[:MAX_POSTS_PER_RUN]:
        if send(build_message(name, item)):
            posted += 1
            seen_list.append(k)
        time.sleep(0 if DRY_RUN else SECONDS_BETWEEN_POSTS)
    save_seen(seen_list)
    print(f"Posted {posted} new item(s). Waiting for next run: {len(new_posts) - posted}.")


if __name__ == "__main__":
    main()
