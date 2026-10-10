"""Headline handling: pure functions, no network. Titles, links, source names and timestamps only.

The title normalisation, Jaccard de-duplication, recency windows and priority bucketing are adapted from MarketBrief
(github.com/yukipanpan/marketbrief, MIT, Copyright (c) 2025 yukipan), see THIRD_PARTY_NOTICES.md. Differences: an item
without a date is excluded and counted (never treated as fresh), and nothing is sampled at random.
"""
import hashlib
import re
from datetime import datetime, timedelta

PRIORITY = "official"          # this category is never truncated and is the only one the report prints
NEWS_HOURS, OFFICIAL_HOURS = 36, 168
RELEVANT_CAP, OTHER_CAP = 60, 20
KEYWORDS = frozenset("""ai gold silver crypto bitcoin energy oil fed rate rates inflation gdp market markets semiconductor
china iran treasury yield yields tariff tariffs dollar recession jobs payrolls cpi ppi opec""".split())
STOPWORDS = frozenset("the a an in on at to for of is are was as by with from says said report reports".split())
_analyzer = None


def normalize_title(title):
    """Lowercase, punctuation and stopwords removed, words of 3+ letters: the form headlines are compared in."""
    words = re.sub(r"[^\w\s]", "", (title or "").lower()).split()
    return " ".join(w for w in words if w not in STOPWORDS and len(w) > 2)


def word_overlap(a, b):
    """Jaccard similarity of two strings' word sets."""
    wa, wb = set(a.split()), set(b.split())
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def vader_score(title):
    global _analyzer
    if _analyzer is None:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        _analyzer = SentimentIntensityAnalyzer()
    return round(_analyzer.polarity_scores(title)["compound"], 2)


def _ts(iso):
    return datetime.fromisoformat(iso) if iso else None


def dedup(items, threshold=0.55):
    """Merge items about one event. items: {title, url, source, category, published_at (ISO or None)}.
    A cluster keeps every distinct URL; it is `official` category if any member is."""
    normed = [(normalize_title(i["title"]), i) for i in items]
    used, out = [False] * len(normed), []
    for i, (ni, first) in enumerate(normed):
        if used[i]:
            continue
        used[i] = True
        cluster = [first]
        for j in range(i + 1, len(normed)):
            if not used[j] and word_overlap(ni, normed[j][0]) >= threshold:
                used[j] = True
                cluster.append(normed[j][1])
        best = max(cluster, key=lambda x: len(x["title"]))
        sources, seen = [], set()
        for c in cluster:
            if c["url"] not in seen:
                seen.add(c["url"])
                sources.append({"name": c["source"], "url": c["url"]})
        known = [c["published_at"] for c in cluster if c.get("published_at")]
        out.append({"title": best["title"], "published_at": max(known, key=_ts) if known else None,
                    "category": PRIORITY if any(c["category"] == PRIORITY for c in cluster) else best["category"],
                    "sources": sources, "sentiment": vader_score(best["title"]),
                    "cluster_key": hashlib.sha1(normalize_title(best["title"]).encode()).hexdigest()[:12]})
    return out


def window(items, now, hours):
    """(kept, unknown): items published within `hours` of now. An item with no date is excluded and counted."""
    cutoff = now - timedelta(hours=hours)
    kept = [i for i in items if i.get("published_at") and _ts(i["published_at"]) >= cutoff]
    return kept, sum(1 for i in items if not i.get("published_at"))


def _newest(rows):
    return sorted(rows, key=lambda c: (_ts(c["published_at"]), c["cluster_key"]), reverse=True)


def bucket(clusters):
    """Priority clusters in full; of the rest the newest keyword-relevant ones, then the newest others (capped)."""
    pri = [c for c in clusters if c["category"] == PRIORITY]
    rest = [c for c in clusters if c["category"] != PRIORITY]
    rel = [c for c in rest if KEYWORDS & set(normalize_title(c["title"]).split())]
    other = [c for c in rest if c not in rel]
    return _newest(pri) + _newest(rel)[:RELEVANT_CAP] + _newest(other)[:OTHER_CAP]
