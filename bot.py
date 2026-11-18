"""
NSE filings -> Telegram channel bot.

Every run it reads NSE's official RSS feeds, keeps only the important filing
types you switched on below, only for companies above your market-cap limit,
and posts the ones it has not posted before.

You only ever need to edit the SETTINGS section.
"""

import csv
import glob
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

# =================================================================
# SETTINGS  (change True to False to switch a category off)
# =================================================================
MIN_MARKET_CAP_CRORE = 500      # only companies bigger than this
POST_UNKNOWN_COMPANIES = False  # True = also post companies missing from the market-cap list

SWITCHES = {
    "results":     True,   # 📊 Quarterly / annual results
    "order":       True,   # 📦 Order wins and contracts
    "deal":        True,   # 🤝 Acquisitions, mergers, demergers, JVs, stake sales
    "fundraise":   True,   # 💵 QIP, preferential issue, rights issue, NCDs, warrants
    "outcome":     True,   # ✅ Board decisions: bonus, split, buyback, fundraise
    "board":       True,   # 🗓 Upcoming board meetings on those same big topics
    "corp_action": True,   # 💰 Bonus, split, buyback, rights (no dividends / ex-dates)
    "management":  True,   # 👔 CEO / MD / CFO / Chairman / auditor changes
    "expansion":   True,   # 🏭 New plants, commercial production, capex, MoUs
    "regulatory":  True,   # 💊 USFDA approvals, warning letters, inspections
    "rating":      True,   # ⭐ Credit rating upgrades / downgrades
    "red_flag":    True,   # ⚠️ Defaults, fraud, raids, insolvency, forensic audits
}
# =================================================================

FEEDS = {
    "results": "https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml",
    "announcements": "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml",
    "board": "https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml",
    "corp_action": "https://nsearchives.nseindia.com/content/RSS/Corporate_action.xml",
}

LABELS = {
    "results": "📊 RESULTS",
    "order": "📦 ORDER WIN",
    "deal": "🤝 DEAL / M&A",
    "fundraise": "💵 FUND RAISING",
    "outcome": "✅ BOARD DECISION",
    "board": "🗓 BOARD MEETING",
    "corp_action": "💰 CORPORATE ACTION",
    "management": "👔 MANAGEMENT CHANGE",
    "expansion": "🏭 EXPANSION",
    "regulatory": "💊 REGULATORY",
    "rating": "⭐ CREDIT RATING",
    "red_flag": "⚠️ RED FLAG",
}

# Big corporate events. Used for board meetings, board outcomes and corporate actions.
BIG_EVENT_WORDS = [
    "bonus", "split", "sub-division", "subdivision", "buyback", "buy back",
    "fund raising", "fundraising", "raising of funds", "raise funds", "qip",
    "qualified institution", "preferential", "rights issue", "rights entitlement",
    "merger", "amalgamation", "demerger", "acquisition", "scheme of arrangement",
    "delisting", "capital reduction", "spin off", "spin-off",
]

# Words that mean the filing is a routine shareholding disclosure, not a deal.
DISCLOSURE_WORDS = [
    "sast", "regulation 29", "reg. 29", "reg 29", "regulation 31", "reg. 31",
    "substantial acquisition", "insider trading", "regulation 7", "pledge",
    "encumbrance",
]

# Announcement categories, checked top to bottom; each filing gets the first match.
#   any  = at least one of these words must appear
#   also = (optional) at least one of these must ALSO appear
#   skip = none of these may appear
ANNOUNCEMENT_RULES = [
    ("red_flag", {
        "any": ["default in payment", "defaulted", "default on", "fraud", "search and seizure",
                "search operation", "raid", "forensic audit", "insolvency", "cirp",
                "resolution professional", "auditor has resigned", "resignation of statutory auditor",
                "resignation of auditor", "suspension of trading", "wilful defaulter"],
        "skip": [],
    }),
    ("regulatory", {
        "any": ["usfda", "us fda", "u.s. fda", "warning letter", "import alert", "form 483",
                "form-483", "establishment inspection", "eir", "anda", "cdsco", "ema approval",
                "who-gmp", "official action indicated"],
        "skip": [],
    }),
    ("order", {
        "any": ["bagging", "receiving of order", "order", "contract", "letter of award",
                "letter of intent", "loa", "work order", "purchase order", "l1 bidder",
                "lowest bidder"],
        "skip": ["court", "tribunal", "nclt", "nclat", "sebi", "penalty", "gst", "tax",
                 "demand", "show cause", "arbitration", "adjudicat", "regulator", "fine",
                 "rbi", "order sheet", "winding up", "insolvency", "interim order",
                 "in order to", "standing order", "court order"],
    }),
    ("deal", {
        "any": ["acquisition", "acquire", "amalgamation", "merger", "scheme of arrangement",
                "demerger", "takeover", "open offer", "stake sale", "divestment",
                "disinvestment", "slump sale", "joint venture", "strategic investment",
                "sale of subsidiary", "sale of stake"],
        "skip": DISCLOSURE_WORDS,
    }),
    ("fundraise", {
        "any": ["qip", "qualified institution", "preferential issue", "preferential allotment",
                "rights issue", "fund raising", "raising of funds", "warrants",
                "non-convertible debenture", "ncd", "fpo", "follow on public offer",
                "further public offer"],
        "skip": ["esop", "esos", "esps", "employee stock", "employees stock"],
    }),
    ("outcome", {
        "any": ["outcome of board meeting", "outcome of the board meeting", "board approved",
                "board has approved", "board of directors approved"],
        "also": BIG_EVENT_WORDS,
        "skip": [],
    }),
    ("management", {
        "any": ["resignation", "resigned", "appointment", "appointed", "cessation",
                "re-designation", "change in management", "step down", "stepped down"],
        "also": ["managing director", "chief executive", "ceo", "chief financial", "cfo",
                 "chairman", "chairperson", "statutory auditor", "whole time director",
                 "whole-time director"],
        "skip": ["company secretary and compliance officer only"],
    }),
    ("expansion", {
        "any": ["commercial production", "commencement of production", "commissioning",
                "commissioned", "capacity expansion", "expansion of capacity", "new plant",
                "new facility", "new manufacturing", "greenfield", "brownfield", "capex",
                "capital expenditure", "memorandum of understanding", "mou",
                "strategic partnership", "technology partnership"],
        "skip": [],
    }),
    ("rating", {
        "any": ["credit rating", "rating agency", "icra", "crisil", "care ratings",
                "india ratings", "brickwork", "acuite", "infomerics"],
        "also": ["upgrade", "upgraded", "downgrade", "downgraded", "negative", "watch",
                 "revised", "revision"],
        "skip": [],
    }),
]

STATE_FILE = "posted.json"
MAX_POSTS_PER_RUN = 25
KEEP_IDS = 8000
FIRST_KINDS = ["results", "order", "board", "corp_action"]  # categories of the first version

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "").strip()
TEST_FEED_DIR = os.environ.get("TEST_FEED_DIR")  # only used for local testing
DRY_RUN = os.environ.get("DRY_RUN") == "1"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}


# ---------------- market-cap list ----------------

def norm_name(name):
    n = name.lower().replace("&", " and ")
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = re.sub(r"\b(limited|ltd|the|india|co|company|corporation|corp)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def read_rows(path):
    p = path.lower()
    if p.endswith(".csv"):
        with open(path, encoding="utf-8-sig", errors="ignore") as f:
            return [r for r in csv.reader(f)]
    if p.endswith(".xlsx"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        return [list(r) for r in wb.active.iter_rows(values_only=True)]
    if p.endswith(".xls"):
        import xlrd
        sh = xlrd.open_workbook(path).sheet_by_index(0)
        return [sh.row_values(i) for i in range(sh.nrows)]
    return []


def to_number(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^0-9.]", "", str(v))
    try:
        return float(s) if s else None
    except ValueError:
        return None


def load_market_caps():
    """Finds the NSE market-cap file you uploaded and returns lookups in crore."""
    files = [f for f in glob.glob("*") if f.lower().endswith((".xlsx", ".xls", ".csv"))]
    if not files:
        print("No market-cap file found, so the market-cap filter is OFF. See setup notes.")
        return None
    path = max(files, key=os.path.getmtime)
    try:
        rows = read_rows(path)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open market-cap file {path}: {e}. Market-cap filter is OFF.")
        return None

    # find the header row
    for hi, row in enumerate(rows[:30]):
        cells = [str(c or "").lower() for c in row]
        sym = next((i for i, c in enumerate(cells) if "symbol" in c), None)
        name = next((i for i, c in enumerate(cells) if "name" in c), None)
        cap = next((i for i, c in enumerate(cells) if "market cap" in c or "mcap" in c
                    or "market capitali" in c), None)
        if cap is not None and (sym is not None or name is not None):
            break
    else:
        print(f"Could not find symbol / name / market cap columns in {path}. Filter is OFF.")
        return None

    header = str(rows[hi][cap] or "").lower()
    divide = 100.0 if "lakh" in header or "lac" in header else 1.0   # lakhs -> crore
    by_sym, by_name = {}, {}
    for row in rows[hi + 1:]:
        if cap >= len(row):
            continue
        val = to_number(row[cap])
        if val is None:
            continue
        crore = val / divide
        if sym is not None and sym < len(row) and row[sym]:
            by_sym[str(row[sym]).strip().upper()] = crore
        if name is not None and name < len(row) and row[name]:
            by_name[norm_name(str(row[name]))] = crore
    unit = "lakhs" if divide == 100 else "crore"
    print(f"Market-cap list: {path} ({len(by_sym) or len(by_name)} companies, read as Rs {unit}).")
    return {"sym": by_sym, "name": by_name}


def symbol_from_link(link):
    # NSE attachment names usually start with the stock symbol, e.g. BEL_17112026....pdf
    m = re.search(r"/([A-Z0-9&\-]+)_\d", link or "")
    return m.group(1) if m else None


def market_cap_of(item, caps):
    sym = symbol_from_link(item["link"])
    if sym and sym in caps["sym"]:
        return caps["sym"][sym]
    return caps["name"].get(norm_name(item["company"]))


# ---------------- reading feeds ----------------

def fetch(name, url):
    if TEST_FEED_DIR:
        path = os.path.join(TEST_FEED_DIR, name + ".xml")
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return f.read()
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            print(f"Could not read {name} feed (try {attempt + 1}/3): {e}")
            time.sleep(5)
    return None


def text(item, tag):
    el = item.find(tag)
    return (el.text or "").strip() if el is not None and el.text else ""


def parse(raw):
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"Feed was not valid XML: {e}")
        return []
    items = []
    for it in root.iter("item"):
        items.append({
            "company": text(it, "title"),
            "desc": re.sub(r"\s+", " ", html.unescape(text(it, "description"))).strip(),
            "link": text(it, "link"),
            "date": text(it, "pubDate"),
        })
    return items


# ---------------- choosing what to post ----------------

def has_word(d, w):
    # short words like "ceo", "mou", "loa" must match as whole words
    pattern = r"\b" + re.escape(w)
    if len(w) <= 4:
        pattern += r"\b"
    return re.search(pattern, d) is not None


def has_any(desc, words):
    d = desc.lower()
    return any(has_word(d, w) for w in words)


def classify(desc):
    for kind, rule in ANNOUNCEMENT_RULES:
        if not SWITCHES.get(kind):
            continue
        if not has_any(desc, rule["any"]):
            continue
        if rule.get("also") and not has_any(desc, rule["also"]):
            continue
        if rule.get("skip") and has_any(desc, rule["skip"]):
            continue
        return kind
    return None


def day_of(date_str):
    m = re.match(r"(\d{1,2}-[A-Za-z]{3}-\d{4})", date_str)
    return m.group(1) if m else date_str[:16]


def make_id(kind, item):
    if kind == "results":
        # one post per company per day (standalone + consolidated = one post)
        return f"results|{item['company'].lower()}|{day_of(item['date'])}"
    return f"{kind}|{item['link'] or item['company'] + item['desc'][:80]}"


def collect():
    found = []
    if SWITCHES["results"]:
        raw = fetch("results", FEEDS["results"])
        if raw:
            found += [("results", i) for i in parse(raw)]
    raw = fetch("announcements", FEEDS["announcements"])
    if raw:
        for i in parse(raw):
            kind = classify(i["desc"])
            if kind:
                found.append((kind, i))
    if SWITCHES["board"]:
        raw = fetch("board", FEEDS["board"])
        if raw:
            found += [("board", i) for i in parse(raw) if has_any(i["desc"], BIG_EVENT_WORDS)]
    if SWITCHES["corp_action"]:
        raw = fetch("corp_action", FEEDS["corp_action"])
        if raw:
            found += [("corp_action", i) for i in parse(raw)
                      if has_any(i["desc"], BIG_EVENT_WORDS)]
    return found


def apply_market_cap(found, caps):
    if caps is None:
        return [(k, i, None) for k, i in found]
    kept, unknown = [], set()
    for kind, item in found:
        cap = market_cap_of(item, caps)
        if cap is None:
            unknown.add(item["company"])
            if POST_UNKNOWN_COMPANIES:
                kept.append((kind, item, None))
        elif cap >= MIN_MARKET_CAP_CRORE:
            kept.append((kind, item, cap))
    if unknown:
        print("Not in market-cap list (skipped unless POST_UNKNOWN_COMPANIES is True): "
              + ", ".join(sorted(unknown)[:30]))
    return kept


# ---------------- Telegram ----------------

def fmt_cap(cap):
    n = str(int(round(cap)))
    if len(n) > 3:  # Indian grouping: 3,00,000
        head, tail = n[:-3], n[-3:]
        head = ",".join([head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1])
        n = head + "," + tail
    return f"₹{n} Cr"


def format_post(kind, item, cap):
    e = html.escape
    lines = [f"<b>{LABELS[kind]}</b>", f"<b>{e(item['company'])}</b>"]
    if cap:
        lines[-1] += f"  ·  Mcap {fmt_cap(cap)}"
    if item["desc"]:
        desc = item["desc"]
        if len(desc) > 600:
            desc = desc[:600].rsplit(" ", 1)[0] + "…"
        lines.append(e(desc))
    meta = []
    if item["date"]:
        meta.append(f"🕒 {e(item['date'][:17])}")
    if item["link"]:
        meta.append(f'🔗 <a href="{e(item["link"], quote=True)}">View filing on NSE</a>')
    if meta:
        lines.append(" · ".join(meta))
    lines.append("<i>Source: NSE filings. For information only, not investment advice.</i>")
    return "\n".join(lines)


def send(message):
    if DRY_RUN:
        print("---- would post ----\n" + message + "\n")
        return True
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHANNEL,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
                if json.loads(r.read()).get("ok"):
                    return True
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="ignore")
            print(f"Telegram error {e.code}: {body}")
            if e.code == 429:  # too many messages, wait as Telegram asks
                wait = json.loads(body).get("parameters", {}).get("retry_after", 30)
                time.sleep(int(wait) + 1)
                continue
            return False
        except Exception as e:  # noqa: BLE001
            print(f"Could not reach Telegram: {e}")
            time.sleep(5)
    return False


# ---------------- memory of what was posted ----------------

def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_state(ids, kinds):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"posted": ids[-KEEP_IDS:], "kinds": sorted(kinds)}, f, indent=0)


def main():
    if not DRY_RUN and (not BOT_TOKEN or not CHANNEL):
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHANNEL secret. See setup step 4.")
        sys.exit(1)

    state = load_state()
    caps = load_market_caps()
    found = apply_market_cap(collect(), caps)
    print(f"Found {len(found)} matching filings in the feeds.")

    # First ever run: remember everything already in the feeds, post nothing old.
    if state is None:
        ids = list(dict.fromkeys(make_id(k, i) for k, i, _ in found))
        save_state(ids, LABELS.keys())
        send("✅ Filings bot connected. New NSE filings will appear here automatically.")
        print(f"First run: remembered {len(ids)} existing filings, posted none of them.")
        return

    posted = state.get("posted", [])
    known_kinds = set(state.get("kinds", FIRST_KINDS))
    seen = set(posted)
    new = []
    for kind, item, cap in found:
        i = make_id(kind, item)
        if i in seen:
            continue
        seen.add(i)
        if kind not in known_kinds:
            posted.append(i)   # newly added category: don't flood with old filings
        else:
            new.append((i, kind, item, cap))

    new.reverse()  # feeds list newest first; post oldest first
    count = 0
    for i, kind, item, cap in new:
        if count >= MAX_POSTS_PER_RUN:
            print("Reached the per-run limit; the rest will go out next run.")
            break
        if send(format_post(kind, item, cap)):
            posted.append(i)
            count += 1
            time.sleep(3)
    save_state(posted, known_kinds | set(LABELS.keys()))
    print(f"Posted {count} new filings.")


if __name__ == "__main__":
    main()
