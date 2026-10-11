"""
Job for GitHub Actions. Writes plain JSON files into data/ (served as your "API").

    python gh_job.py live [--dry]      # every 5 min: read homepage only, record changed results
    python gh_job.py sync [slug ...]   # nightly / first run: rebuild history from the chart pages

data/markets.json        latest result of every market
data/<slug>.json         history for one market, newest first
data/state.json          last homepage values seen (used to detect changes)
"""
import json
import os
import re
import sys
import time
from datetime import datetime

from bs4 import BeautifulSoup, NavigableString, Tag

import scraper

DATA = "data"
RESULT_RE = re.compile(r"^([\d*]{3})-([\d*]{2})-([\d*]{3})$")  # 160-77-340, 160-7*-***, ***-**-***


def now_ist():
    return datetime.now(scraper.IST)


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(path, obj, indent=None):
    """Write JSON only if it changed, so unchanged runs make no git commit."""
    text = json.dumps(obj, indent=indent, ensure_ascii=False,
                      separators=(",", ":") if indent is None else None)
    try:
        with open(path) as f:
            if f.read() == text:
                return False
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    return True


def parse_home(html):
    """{slug: (open, jodi, close)} from the homepage.

    Walks the page in order: a result like 160-77-340 is remembered, and the next
    panel-chart link after it tells us which market it belongs to.
    """
    out, current = {}, None
    for node in BeautifulSoup(html, "html.parser").descendants:
        if isinstance(node, NavigableString):
            m = RESULT_RE.match("".join(str(node).split()))
            if m:
                current = m.groups()
        elif isinstance(node, Tag) and node.name == "a":
            m = scraper.PANEL_HREF_RE.search(node.get("href", ""))
            if m and current:
                out.setdefault(m.group(1), current)
                current = None
    return out


def write_index(index):
    save(f"{DATA}/markets.json", sorted(index.values(), key=lambda r: r["market"]), indent=1)


def live(dry=False):
    html = scraper.get(scraper.BASE + "/")
    if not html:
        sys.exit("homepage download failed")
    found = parse_home(html)
    if len(found) < 10:
        sys.exit(f"only {len(found)} markets parsed, homepage layout changed?")

    state = load(f"{DATA}/state.json", {})
    index = {r["market"]: r for r in load(f"{DATA}/markets.json", [])}
    today, seen = now_ist().date().isoformat(), now_ist().isoformat(timespec="seconds")
    changed = []

    for slug, tup in found.items():
        cur = [scraper.clean(x) for x in tup]
        if slug not in state:
            state[slug] = cur  # first sighting: baseline only (homepage may still show yesterday)
            continue
        if all(x is None for x in cur):
            continue  # nothing declared yet
        if state[slug] == cur:
            continue
        state[slug] = cur
        changed.append(slug)
        if dry:
            print(f"changed: {slug} -> {cur}")
            continue
        path = f"{DATA}/{slug}.json"
        rows = [r for r in load(path, []) if r["date"] != today]
        rows.append({"date": today, "open_panna": cur[0], "jodi": cur[1], "close_panna": cur[2], "seen_at": seen})
        rows.sort(key=lambda r: r["date"], reverse=True)
        save(path, rows)
        index[slug] = {"market": slug, **rows[0]}

    print(f"{len(found)} markets on homepage, {len(changed)} changed: {', '.join(changed) or '-'}")
    if dry:
        for slug, tup in list(found.items())[:15]:
            print(f"  {slug}: {'-'.join(tup)}")
        return
    save(f"{DATA}/state.json", state, indent=1)
    write_index(index)


def sync(only=None):
    slugs = only or scraper.list_markets()
    index = {r["market"]: r for r in load(f"{DATA}/markets.json", [])}
    today = now_ist().date()
    for slug in slugs:
        html = scraper.get(f"{scraper.BASE}/panel-chart-record/{slug}.php")
        if not html:
            print(f"{slug}: download failed")
            continue
        chart, bad = [], 0
        for d, op, jodi, cp in scraper.parse_chart(html):
            if d > today or (op is None and jodi is None and cp is None):
                continue
            if op and jodi and cp and jodi != f"{scraper.digit_sum_mod(op)}{scraper.digit_sum_mod(cp)}":
                bad += 1
            chart.append({"date": d.isoformat(), "open_panna": op, "jodi": jodi, "close_panna": cp})
        if not chart:
            print(f"{slug}: nothing parsed")
            continue
        newest = max(r["date"] for r in chart)
        # chart is authoritative; keep only live rows that are newer than the chart
        keep = [r for r in load(f"{DATA}/{slug}.json", []) if r["date"] > newest]
        rows = sorted(keep + chart, key=lambda r: r["date"], reverse=True)
        save(f"{DATA}/{slug}.json", rows)
        index[slug] = {"market": slug, **rows[0]}
        print(f"{slug}: {len(rows)} rows" + (f", {bad} failed the jodi check" if bad else ""))
        time.sleep(scraper.DELAY)
    write_index(index)


if __name__ == "__main__":
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else "live"), sys.argv[2:]
    if cmd == "live":
        live(dry="--dry" in args)
    elif cmd == "sync":
        sync([a for a in args if not a.startswith("--")] or None)
    else:
        sys.exit("usage: gh_job.py live [--dry] | sync [slug ...]")
