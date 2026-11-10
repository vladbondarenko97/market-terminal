"""CHART tab day view headlines: what the world and the markets were talking about on a past date.

Two free archives, fetched once per date through SourceSession.fetch(): Wikipedia's "Portal:Current_events" page for the
day (every day since 2004) and the GDELT DOC 2.0 article list (finance-related English news, 2017 on). Titles, links,
sources and dates only, INSERT OR IGNORE into chart.db v2_news_items. A fetch that found nothing is still logged ok in
v2_fetch_log, which is how a date is known to be done and is never fetched again.
"""
import copy
import hashlib
import json
import re
import time
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

from core import lake, market_calendar, sources

PER_SOURCE = 25
GDELT_FROM = date(2017, 1, 1)
GDELT_PAUSE = 5.0                      # GDELT asks for one request per 5 seconds
GDELT_QUERY = "(economy OR markets OR stocks OR fed) sourcelang:english"
UA = {"User-Agent": "market-terminal/1.0 (personal research; contact via GitHub)"}
MARKET_WORDS = re.compile(r"market|stock|share|index|tariff|trade|fed\b|federal reserve|rate|inflation|econom|oil|opec|"
                          r"bank|bond|treasur|dollar|gold|silver|recession|gdp|jobs|earnings|crash|rall", re.I)
FIRST_CATEGORY = "Business and economy"       # Wikipedia's own heading; its items come first


def _page(day):
    return f"Portal:Current_events/{day.year}_{day.strftime('%B')}_{day.day}"


def _request(provider, day):
    return {"fn": "headlines", "date": day.isoformat()} if provider == "wikipedia" else \
        {"fn": "artlist", "date": day.isoformat(), "query": GDELT_QUERY}


def _wikipedia(day):
    r = requests.get("https://en.wikipedia.org/w/api.php", headers=UA, timeout=30, params={
        "action": "parse", "page": _page(day), "prop": "text", "format": "json", "formatversion": 2, "redirects": 1})
    if r.status_code != 200:
        raise sources.SourceUnavailable("wikipedia", f"http_{r.status_code}", day.isoformat())
    j = r.json()
    if (j.get("error") or {}).get("code") == "missingtitle":
        return {"html": ""}                              # no page for that day: an answer, not a failure
    if "error" in j:
        raise sources.SourceUnavailable("wikipedia", "error", str(j["error"].get("code")))
    return {"html": j["parse"]["text"]}


def _gdelt(day):
    r = requests.get("https://api.gdeltproject.org/api/v2/doc/doc", headers=UA, timeout=45, params={
        "query": GDELT_QUERY, "mode": "artlist", "format": "json", "maxrecords": 50, "sort": "hybridrel",
        "startdatetime": day.strftime("%Y%m%d000000"), "enddatetime": day.strftime("%Y%m%d235959")})
    if r.status_code != 200:
        raise sources.SourceUnavailable("gdelt", f"http_{r.status_code}", day.isoformat())
    return r.json() if r.text.strip() else {}            # no matches is an empty body


def _own(li):
    """A bullet without its sub-bullets: (text, first external link or None)."""
    li = copy.copy(li)
    for sub in li.find_all(["ul", "ol"]):
        sub.decompose()
    a = li.select_one("a.external")
    return " ".join(li.get_text(" ", strip=True).split()), a["href"] if a and a.get("href", "").startswith("http") else None


def parse_wikipedia(html, day):
    """[(title, url, category)] for every bullet under a top-level heading of a Current events page: its own text and first
    external link (else the page itself, with a per-item anchor so the url stays unique). 'Business and economy' comes first."""
    page = f"https://en.wikipedia.org/wiki/{_page(day)}"
    items = []
    for top in BeautifulSoup(html or "", "html.parser").select("ul > li"):
        if top.find_parent("li"):
            continue                                      # nested: reached through its top-level bullet
        cat = _own(top)[0] or None
        for li in top.find_all("li"):
            title, url = _own(li)
            if title:
                items.append((title[:300], url or f"{page}#{hashlib.sha1(title.encode()).hexdigest()[:8]}", cat))
    items.sort(key=lambda x: x[2] != FIRST_CATEGORY)       # stable: page order inside each group
    return items[:PER_SOURCE]


def parse_gdelt(data):
    """[(title, url, domain)] from a GDELT artlist response, in its (hybrid relevance) order."""
    return [(a["title"].strip()[:300], a["url"], a.get("domain")) for a in (data or {}).get("articles") or []
            if a.get("title") and str(a.get("url", "")).startswith("http")][:PER_SOURCE]


def _insert(conn, day, source, items):
    stamp, run = lake.utc_now_iso(), f"chart:{day.isoformat()}"
    with conn:
        before = conn.total_changes
        conn.executemany("""INSERT OR IGNORE INTO v2_news_items (url_hash, title, url, source, category, published_at,
                            first_seen_run, first_seen_at) VALUES (?,?,?,?,?,?,?,?)""",
                         [(hashlib.sha256(u.encode()).hexdigest(), t, u, source, c, day.isoformat(), run, stamp) for t, u, c in items])
        return conn.total_changes - before


PROVIDERS = (("wikipedia", _wikipedia, lambda r, day: parse_wikipedia(r["html"], day)),
             ("gdelt", _gdelt, lambda r, day: parse_gdelt(r)))


def _done(conn, provider, day):
    """The provider answered for `day` (even with nothing), or does not cover it."""
    return (provider == "gdelt" and day < GDELT_FROM) or bool(conn.execute(
        "SELECT 1 FROM v2_fetch_log WHERE source = ? AND request_json = ? AND outcome = 'ok'",
        (provider, lake.dumps(_request(provider, day)))).fetchone())


def is_done(conn, day):
    return all(_done(conn, p, day) for p, _, _ in PROVIDERS)


def collect_day(conn, s, day):
    """Fetch and store one day's headlines from each provider not yet done. Returns {"added": n, "errors": [...]}."""
    added, errors = 0, []
    for p, fetch, parse in PROVIDERS:
        if _done(conn, p, day):
            continue
        try:
            res, _ = s.fetch(p, _request(p, day), lambda f=fetch: f(day), kind="headlines", fmt="json",
                             capture=lambda r: (json.dumps(r).encode(), None, None))
            added += _insert(conn, day, p, parse(res, day))
        except sources.SourceUnavailable as e:
            errors.append(f"{p} {day}: {e}")
        if p == "gdelt":
            time.sleep(GDELT_PAUSE)
    return {"added": added, "errors": errors}


def trading_days(start, end):
    d = start
    while d <= end:
        if market_calendar.is_trading_day(d):
            yield d
        d += timedelta(days=1)


def stored(conn, day, limit=50):
    """[{title, url, source, published_at}] stored for `day` in a connection's v2_news_items (chart.db or portfolio.db)."""
    if conn is None:
        return []
    try:
        rows = conn.execute("""SELECT title, url, source, published_at FROM v2_news_items WHERE substr(published_at, 1, 10) = ?
                               ORDER BY source = 'wikipedia' DESC, rowid LIMIT ?""", (day.isoformat(), limit)).fetchall()
    except Exception:                                      # no such table: nothing stored
        return []
    # ponytail: a keyword rank puts market news above world news; a classifier if this proves too blunt
    return sorted((dict(r) for r in rows), key=lambda r: not MARKET_WORDS.search(r["title"]))
