"""
Scrape panel-chart history for every market into SQLite.

Usage:
    python scraper.py                  # all markets (backfill + update, it's idempotent)
    python scraper.py karnataka-day    # only some markets
    python scraper.py --list           # just print market slugs

Re-running is safe: rows are upserted on (market, date).
"""
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

BASE = "https://dpbossss.com"
DB = "matka.db"
DELAY = 1.0  # seconds between requests, be polite
HEADERS = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"}

LABEL_RE = re.compile(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\s*to\s*(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", re.I)
PANEL_HREF_RE = re.compile(r"/panel-chart-record/([a-z0-9-]+)\.php")
IST = timezone(timedelta(hours=5, minutes=30))


def get(url, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            print(f"  retry {i + 1}/{tries} for {url}: {e}")
            time.sleep(2 * (i + 1))
    return None


def init_db(con):
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS results (
            market      TEXT NOT NULL,
            date        TEXT NOT NULL,   -- YYYY-MM-DD
            open_panna  TEXT,
            jodi        TEXT,
            close_panna TEXT,
            ok          INTEGER,         -- 1 = jodi matches panna sums, 0 = mismatch, NULL = incomplete
            PRIMARY KEY (market, date)
        );
        CREATE INDEX IF NOT EXISTS idx_results_date ON results(date);
        """
    )


def list_markets():
    html = get(BASE + "/")
    if not html:
        sys.exit("could not load homepage")
    slugs = []
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        m = PANEL_HREF_RE.search(a["href"])
        if m and m.group(1) not in slugs:
            slugs.append(m.group(1))
    return slugs


def parse_label_start(text):
    """Start date of 'dd/mm/yyyy to dd/mm/yyyy', or None if it can't be parsed."""
    m = LABEL_RE.search(text)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def clean(field):
    """'160' -> '160', '7*' / '***' -> None"""
    return None if (not field or "*" in field) else field


def digit_sum_mod(s):
    return sum(int(c) for c in s) % 10


def parse_chart(html):
    """Yield (date, open_panna, jodi, close_panna) from a panel chart page."""
    soup = BeautifulSoup(html, "html.parser")
    prev_start = None
    for tr in soup.find_all("tr"):
        text = tr.get_text(" ", strip=True)
        label = LABEL_RE.search(text)
        if not label:
            continue  # header row etc.
        # Everything after the date label: keep only digits and '*'.
        # Each day is exactly 8 chars: 3 (open panna) + 2 (jodi) + 3 (close panna).
        body = re.sub(r"[^0-9*]", "", text[label.end():])
        if not body or len(body) % 8:
            print(f"  skipping odd row ({len(body)} chars): {text[:50]}")
            continue

        # Dates on the site have typos, so don't trust the label after the first row.
        # Use previous week + 7 days; only trust the label for row one (must be a Monday).
        label_start = parse_label_start(text)
        if prev_start is None:
            if not label_start or label_start.weekday() != 0:
                print(f"  first row has a bad date label: {text[:50]}")
                continue
            start = label_start
        else:
            start = prev_start + timedelta(days=7)
            if label_start and label_start.weekday() == 0 and label_start != start:
                print(f"  note: label {label_start} != computed {start}, using computed")
        prev_start = start

        for i in range(len(body) // 8):
            chunk = body[i * 8:(i + 1) * 8]
            yield start + timedelta(days=i), clean(chunk[:3]), clean(chunk[3:5]), clean(chunk[5:])


def save_market(con, slug):
    html = get(f"{BASE}/panel-chart-record/{slug}.php")
    if not html:
        print(f"{slug}: failed to download")
        return 0
    today = datetime.now(IST).date()
    rows, bad = [], 0
    for d, op, jodi, cp in parse_chart(html):
        if d > today:
            continue
        if op is None and jodi is None and cp is None:
            continue  # holiday / no result
        ok = None
        if op and jodi and cp:
            ok = int(jodi == f"{digit_sum_mod(op)}{digit_sum_mod(cp)}")
            bad += 1 - ok
        rows.append((slug, d.isoformat(), op, jodi, cp, ok))
    con.executemany(
        """INSERT INTO results (market, date, open_panna, jodi, close_panna, ok)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(market, date) DO UPDATE SET
             open_panna=excluded.open_panna, jodi=excluded.jodi,
             close_panna=excluded.close_panna, ok=excluded.ok""",
        rows,
    )
    con.commit()
    print(f"{slug}: {len(rows)} results saved" + (f", {bad} failed the jodi check" if bad else ""))
    return len(rows)


def main():
    args = sys.argv[1:]
    if args == ["--list"]:
        print("\n".join(list_markets()))
        return
    con = sqlite3.connect(DB)
    init_db(con)
    slugs = args or list_markets()
    print(f"{len(slugs)} markets")
    for slug in slugs:
        save_market(con, slug)
        time.sleep(DELAY)
    con.close()


if __name__ == "__main__":
    main()
