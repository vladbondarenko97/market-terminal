"""Shared, per-run source collectors.

Every provider call goes through SourceSession.fetch(): identical requests in one run are fetched once,
calls are bounded per provider, outcomes are logged, and the raw response is captured losslessly in
v2_payloads. Offline mode makes zero provider calls.
"""
import io
import json
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import pandas as pd

from core import lake

PROVIDER_LIMITS = {"eia": 5, "tceq": 2, "cftc": 2, "yahoo": 2, "fred": 2, "forexfactory": 1, "databento": 1, "goldapi": 1, "akshare": 1,
                   "ebay": 1, "cme": 1}


class SourceUnavailable(Exception):
    def __init__(self, source, outcome, detail=""):
        super().__init__(f"{source}: {outcome} {detail}".strip())
        self.source, self.outcome, self.detail = source, outcome, detail


class SourceSession:
    def __init__(self, conn, run_id, *, offline=False):
        self.conn = conn
        self.run_id = run_id
        self.offline = offline
        self._memo = {}
        self._locks = {}
        self._sem = {p: threading.Semaphore(n) for p, n in PROVIDER_LIMITS.items()}
        self._guard = threading.Lock()
        self.calls = {}           # provider -> attempted network calls
        self.cache_hits = {}      # provider -> dedup hits
        self.errors = []
        self.payload_bytes = 0

    def _count(self, table, provider):
        with self._guard:
            table[provider] = table.get(provider, 0) + 1

    def fetch(self, provider, request, fn, *, kind, fmt, capture, source_date=None):
        """fn() -> result. capture(result) -> (bytes, parsed_or_None, coverage_or_None).
        Returns (result, payload_id)."""
        key = lake.dumps({"p": provider, "r": request})
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            if key in self._memo:
                self._count(self.cache_hits, provider)
                ok, value = self._memo[key]
                if ok:
                    return value
                raise value
            if self.offline:
                err = SourceUnavailable(provider, "offline", "network disabled for this run")
                self._memo[key] = (False, err)
                raise err
            started = lake.utc_now_iso()
            t0 = time.monotonic()
            self._count(self.calls, provider)
            with self._sem.get(provider, threading.Semaphore(1)):
                try:
                    result = fn()
                except Exception as e:
                    elapsed = int((time.monotonic() - t0) * 1000)
                    detail = lake.redact(f"{type(e).__name__}: {e}")
                    lake.log_fetch(self.conn, run_id=self.run_id, source=provider, request=request,
                                   started_at=started, elapsed_ms=elapsed, outcome="error", detail=detail)
                    self.errors.append({"source": provider, "request": request, "error": detail[:500]})
                    err = e if isinstance(e, SourceUnavailable) else SourceUnavailable(provider, "error", detail[:300])
                    self._memo[key] = (False, err)
                    raise err
            elapsed = int((time.monotonic() - t0) * 1000)
            payload_id = None
            outcome = "ok"
            try:
                content, parsed, coverage = capture(result)
                if content is not None:
                    self.payload_bytes += len(content)
                    payload_id = lake.store_payload(
                        self.conn, source=provider, kind=kind, content=content, fmt=fmt, request=request,
                        fetched_at=started, source_date=source_date, parsed=parsed, coverage=coverage,
                        run_id=self.run_id)
                else:
                    outcome = "ok_not_captured" if kind == "auth" else "empty"
            except Exception as e:  # capture failure must be visible, not masked
                self.errors.append({"source": provider, "request": request, "error": lake.redact(f"capture failed: {e}")})
                outcome = "capture_failed"
            lake.log_fetch(self.conn, run_id=self.run_id, source=provider, request=request, started_at=started,
                           elapsed_ms=elapsed, outcome=outcome, payload_id=payload_id)
            value = (result, payload_id)
            self._memo[key] = (True, value)
            return value

    def stats(self):
        return {"calls": dict(self.calls), "dedup_hits": dict(self.cache_hits), "errors": list(self.errors),
                "payload_bytes": self.payload_bytes}


# ---------------------------------------------------------------- capture helpers
def _frame_capture(df):
    if df is None:
        return None, None, None
    buf = io.StringIO()
    df.to_csv(buf)
    return buf.getvalue().encode(), None, {"rows": int(len(df))}


# ---------------------------------------------------------------- Yahoo
def yf_history(s, symbol, period="7d", interval="1d"):
    import yfinance as yf

    def call():
        df = yf.Ticker(symbol).history(period=period, interval=interval)
        if df is None or df.empty:
            raise SourceUnavailable("yahoo", "empty", f"{symbol} {period}/{interval}")
        return df
    df, pid = s.fetch("yahoo", {"fn": "history", "symbol": symbol, "period": period, "interval": interval}, call,
                      kind="ohlcv", fmt="csv", capture=_frame_capture)
    return df, pid


def yf_quote(s, symbol):
    """Last close + change vs previous close from a 7-day daily history (weekend-safe)."""
    try:
        df, pid = yf_history(s, symbol, "7d", "1d")
    except SourceUnavailable as e:
        return {"symbol": symbol, "value": None, "change": None, "status": "missing", "reason": str(e),
                "source": "yahoo"}
    closes = df["Close"].dropna()
    if closes.empty:
        return {"symbol": symbol, "value": None, "change": None, "status": "missing",
                "reason": "no closes", "source": "yahoo", "payload_id": pid}
    last_ts = closes.index[-1]
    return {"symbol": symbol, "value": float(closes.iloc[-1]),
            "change": float(closes.iloc[-1] - closes.iloc[-2]) if len(closes) > 1 else None,
            "prev_close": float(closes.iloc[-2]) if len(closes) > 1 else None,
            "observed_at": pd.Timestamp(last_ts).isoformat(), "status": "fresh", "reason": None,
            "source": "yahoo", "payload_id": pid}


def yf_option_chains(s, symbol):
    """Every current expiration, each fetched once; reused for GEX, max pain and selections."""
    import yfinance as yf
    t = yf.Ticker(symbol)

    def list_exps():
        exps = list(t.options or [])
        if not exps:
            raise SourceUnavailable("yahoo", "empty", f"no expirations for {symbol}")
        return exps
    exps, _ = s.fetch("yahoo", {"fn": "options", "symbol": symbol}, list_exps, kind="option_expirations",
                      fmt="json", capture=lambda r: (json.dumps(r).encode(), None, {"expirations": len(r)}))
    chains, payloads, failed = {}, {}, []
    for exp in exps:
        def call(exp=exp):
            ch = t.option_chain(exp)
            return {"calls": ch.calls, "puts": ch.puts}

        def cap(r, exp=exp):
            c = r["calls"].assign(side="call")
            p = r["puts"].assign(side="put")
            both = pd.concat([c, p], ignore_index=True)
            both["expiration"] = exp
            return both.to_csv(index=False).encode(), None, {"calls": len(c), "puts": len(p)}
        try:
            r, pid = s.fetch("yahoo", {"fn": "option_chain", "symbol": symbol, "expiration": exp}, call,
                             kind="option_chain", fmt="csv", capture=cap)
            chains[exp] = r
            payloads[exp] = pid
        except SourceUnavailable as e:
            failed.append({"expiration": exp, "error": str(e)})
    return {"expirations": exps, "chains": chains, "payloads": payloads, "failed": failed}


# ---------------------------------------------------------------- FRED
def fred_series(s, series_id, api_key, limit=300):
    import requests
    if not api_key:
        raise SourceUnavailable("fred", "not_configured", "FRED_API_KEY missing")

    def call():
        for attempt in range(2):                      # one bounded retry for transient 5xx
            r = requests.get("https://api.stlouisfed.org/fred/series/observations", timeout=15, params={
                "series_id": series_id, "api_key": api_key, "file_type": "json", "sort_order": "desc", "limit": limit})
            if r.status_code < 500 or attempt == 1:
                break
            time.sleep(2)
        if r.status_code != 200:
            raise SourceUnavailable("fred", f"http_{r.status_code}", series_id)
        return r.json()

    def cap(j):
        return json.dumps(j).encode(), None, {"observations": len(j.get("observations", []))}
    data, pid = s.fetch("fred", {"series_id": series_id, "limit": limit, "sort": "desc"}, call, kind="fred_series",
                        fmt="json", capture=cap)
    obs = [{"date": o["date"], "value": float(o["value"])} for o in data.get("observations", []) if o["value"] != "."]
    return obs, pid


def fred_deltas(obs, divisor=1.0):
    """Legacy offsets in valid observations (1D=1, 1W=5, 1M=21, 1Y=252). Missing history -> None."""
    if not obs:
        return None
    vals = [o["value"] / divisor for o in obs]
    latest = vals[0]

    def back(n):
        return latest - vals[n] if len(vals) > n else None
    return {"latest": latest, "date": obs[0]["date"], "1D": back(1), "1W": back(5), "1M": back(21), "1Y": back(252)}


# ---------------------------------------------------------------- ForexFactory calendar
FF_URLS = {"thisweek": "https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
           "nextweek": "https://nfs.faireconomy.media/ff_calendar_nextweek.xml"}


def ff_calendar(s, week):
    import requests

    def call():
        r = requests.get(FF_URLS[week], headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        if r.status_code != 200:
            raise SourceUnavailable("forexfactory", f"http_{r.status_code}", week)
        root = ET.fromstring(r.content)   # validity is decided by parsing, not by Content-Type
        if root.tag != "weeklyevents" and not root.findall("event"):
            raise SourceUnavailable("forexfactory", "invalid", "not a calendar feed")
        return r.content

    def cap(content):
        return content, None, None
    content, pid = s.fetch("forexfactory", {"week": week}, call, kind="calendar_xml", fmt="xml", capture=cap)
    events = []
    for ev in ET.fromstring(content).findall("event"):
        events.append({k: (ev.findtext(k) or "").strip() for k in
                       ("title", "country", "date", "time", "impact", "forecast", "previous", "actual", "url")})
    return events, pid


# ---------------------------------------------------------------- GoldAPI / SGE
def goldapi_spot(s, api_key, metal="XAG"):
    import requests
    if not api_key:
        raise SourceUnavailable("goldapi", "not_configured", "GOLD_API_KEY missing")

    def call():
        r = requests.get(f"https://www.goldapi.io/api/{metal}/USD", timeout=10,
                         headers={"x-access-token": api_key, "Content-Type": "application/json"})
        if r.status_code != 200:
            raise SourceUnavailable("goldapi", f"http_{r.status_code}", r.text[:200])
        j = r.json()
        if j.get("price") is None:
            raise SourceUnavailable("goldapi", "invalid", "no price field")
        return j
    j, pid = s.fetch("goldapi", {"metal": metal}, call, kind="spot_quote", fmt="json",
                     capture=lambda r: (json.dumps(r).encode(), None, None))
    ts = j.get("timestamp")
    return {"value": float(j["price"]), "change": j.get("ch"),
            "observed_at": datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None,
            "status": "fresh", "reason": None, "source": "goldapi", "payload_id": pid}


def _with_timeout(fn, seconds, source, what):
    """Run a library call that has no timeout of its own on a daemon thread; give up after `seconds`."""
    box = {}

    def target():
        try:
            box["value"] = fn()
        except Exception as e:           # surfaced below
            box["error"] = e
    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(seconds)
    if th.is_alive():
        raise SourceUnavailable(source, "timeout", f"{what} exceeded {seconds}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def sge_silver_benchmark(s):
    def call():
        import akshare as ak
        df = _with_timeout(ak.spot_silver_benchmark_sge, 45, "akshare", "SGE benchmark")
        if df is None or df.empty:
            raise SourceUnavailable("akshare", "empty", "SGE benchmark")
        return df
    df, pid = s.fetch("akshare", {"fn": "spot_silver_benchmark_sge"}, call, kind="sge_benchmark", fmt="csv",
                      capture=lambda d: (d.to_csv(index=False).encode(), None, {"rows": len(d)}))
    latest = df.iloc[-1]
    cols = list(df.columns)
    date_col = cols[0]
    evening = latest.get("晚盘价")
    morning = latest.get("早盘价")
    use_evening = evening is not None and pd.notna(evening) and float(evening) > 0
    price = float(evening) if use_evening else (float(morning) if morning is not None and pd.notna(morning) else None)
    return {"value": price, "session": "evening" if use_evening else "morning", "date": str(latest[date_col]),
            "unit": "CNY/kg", "status": "fresh" if price else "missing", "source": "akshare:SGE",
            "payload_id": pid}


# ---------------------------------------------------------------- Databento
def databento_window(now=None):
    """Legacy T+1 window: previous complete UTC day, skipping weekends."""
    now = now or datetime.now(timezone.utc)
    end = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if end.weekday() == 6:
        end -= timedelta(days=1)
    elif end.weekday() == 0:
        end -= timedelta(days=2)
    return end


def databento_trades(s, api_key, symbol, start, end, limit, dataset="DBEQ.BASIC"):
    if not api_key:
        raise SourceUnavailable("databento", "not_configured", "DATABENTO_API_KEY missing")

    def call():
        import databento as db
        client = db.Historical(api_key)
        data = client.timeseries.get_range(dataset=dataset, schema="trades", symbols=[symbol],
                                           start=start.isoformat(), end=end.isoformat(), limit=limit)
        return data.to_df()
    req = {"dataset": dataset, "schema": "trades", "symbol": symbol, "start": start.isoformat(),
           "end": end.isoformat(), "limit": limit}
    df, pid = s.fetch("databento", req, call, kind="trades", fmt="csv",
                      capture=lambda d: (d.to_csv().encode(), None, {"rows": len(d), "limit": limit,
                                                                     "limit_hit": len(d) >= limit}))
    return df, pid


# ---------------------------------------------------------------- eBay
def ebay_listings(s, app_id, cert_id, item_ids):
    import base64
    import requests
    if not app_id or not cert_id:
        raise SourceUnavailable("ebay", "not_configured", "EBAY_APP_ID/EBAY_CERT_ID missing")

    def token_call():
        auth = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
        r = requests.post("https://api.ebay.com/identity/v1/oauth2/token", timeout=15,
                          headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {auth}"},
                          data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"})
        r.raise_for_status()
        return r.json()["access_token"]
    # The token is a credential: never captured.
    token, _ = s.fetch("ebay", {"fn": "oauth_token"}, token_call, kind="auth", fmt="none",
                       capture=lambda r: (None, None, None))
    listings, errors = [], []
    for item_id in item_ids:
        def call(item_id=item_id):
            r = requests.get("https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id", timeout=15,
                             params={"legacy_item_id": item_id},
                             headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"})
            if r.status_code == 404:
                raise SourceUnavailable("ebay", "not_found", item_id)
            r.raise_for_status()
            return r.json()
        try:
            j, pid = s.fetch("ebay", {"fn": "get_item_by_legacy_id", "item_id": item_id}, call, kind="listing",
                             fmt="json", capture=lambda r: (json.dumps(r).encode(), None, None))
            listings.append({"item_id": item_id, "raw": j, "payload_id": pid})
        except SourceUnavailable as e:
            errors.append({"item_id": item_id, "error": str(e)})
        time.sleep(0.1)
    return listings, errors


# ---------------------------------------------------------------- Forecast Lab sources
def cftc_cot(s, dataset, contract_code, since):
    """CFTC Commitments of Traders via the public Socrata API (no key). dataset: 72hh-3qpy (disaggregated
    futures-only) or gpe5-46if (traders in financial futures, futures-only)."""
    import requests

    def call():
        r = requests.get(f"https://publicreporting.cftc.gov/resource/{dataset}.json", timeout=30, params={
            "$where": f"cftc_contract_market_code='{contract_code}' AND report_date_as_yyyy_mm_dd >= '{since}'",
            "$order": "report_date_as_yyyy_mm_dd ASC", "$limit": 5000})
        r.raise_for_status()
        rows = r.json()
        if not rows:
            raise SourceUnavailable("cftc", "empty", f"{dataset}/{contract_code}")
        return rows
    rows, pid = s.fetch("cftc", {"dataset": dataset, "code": contract_code, "since": since}, call, kind="cot",
                        fmt="json", capture=lambda r: (json.dumps(r).encode(), None, {"rows": len(r)}))
    return rows, pid


def ishares_slv(s):
    """Ounces in trust and premium/discount from the iShares SLV product page."""
    import html as _html
    import re
    import requests

    def call():
        r = requests.get("https://www.ishares.com/us/products/239855/ishares-silver-trust-fund",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        r.raise_for_status()
        return r.text
    text, pid = s.fetch("ishares", {"page": "SLV"}, call, kind="etf_page", fmt="html",
                        capture=lambda t: (t.encode(), None, None))
    t = _html.unescape(text)

    def field(label):
        m = re.search(re.escape(label) + r'","formattedValue":"([-\d.,]+)"', t)
        if not m:
            return None, None
        d = re.search(r'"formattedAsOfDate":"([^"]+)"', t[m.end():m.end() + 600])
        return float(m.group(1).replace(",", "")), (d.group(1) if d else None)
    oz, oz_date = field("Ounces in Trust")
    prem, prem_date = field("Premium/Discount")
    if oz is None:
        raise SourceUnavailable("ishares", "parse", "ounces in trust not found")
    from datetime import datetime as _dt
    return {"ounces_in_trust": oz, "as_of": _dt.strptime(oz_date, "%b %d, %Y").date().isoformat() if oz_date else None,
            "premium_discount_pct": prem, "status": "fresh", "source": "ishares.com", "payload_id": pid}


def fomc_calendar(s):
    """FOMC decision dates (last day of each meeting) from federalreserve.gov."""
    import re
    import requests
    from datetime import date as _date

    def call():
        r = requests.get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        r.raise_for_status()
        return r.text
    text, pid = s.fetch("federalreserve", {"page": "fomccalendars"}, call, kind="fomc_calendar", fmt="html",
                        capture=lambda t: (t.encode(), None, None))
    months = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August",
                                          "September", "October", "November", "December"], 1)}
    dates = []
    parts = re.split(r"(\d{4}) FOMC Meetings", text)
    for i in range(1, len(parts) - 1, 2):
        year, block = int(parts[i]), parts[i + 1]
        for mon, days in re.findall(r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>.*?fomc-meeting__date[^>]*>([^<]+)<',
                                    block, re.S):
            last_mon = mon.split("/")[-1].strip()
            nums = re.findall(r"\d+", days)
            if last_mon in months and nums:
                try:
                    dates.append(_date(year, months[last_mon], int(nums[-1])).isoformat())
                except ValueError:
                    pass
    if len(dates) < 8:
        raise SourceUnavailable("federalreserve", "parse", f"only {len(dates)} FOMC dates parsed")
    return sorted(set(dates)), pid


def fred_release_dates(s, release_id, api_key, start):
    """Scheduled + past release dates for a FRED release (e.g. 10 = CPI)."""
    import requests
    if not api_key:
        raise SourceUnavailable("fred", "not_configured", "FRED_API_KEY missing")

    def call():
        r = requests.get("https://api.stlouisfed.org/fred/release/dates", timeout=20, params={
            "release_id": release_id, "api_key": api_key, "file_type": "json", "realtime_start": start,
            "include_release_dates_with_no_data": "true", "limit": 1000, "sort_order": "asc"})
        r.raise_for_status()
        return r.json()
    j, pid = s.fetch("fred", {"release_dates": release_id, "start": start}, call, kind="fred_release_dates",
                     fmt="json", capture=lambda r: (json.dumps(r).encode(), None, None))
    return sorted({d["date"] for d in j.get("release_dates", [])}), pid


def yf_total_assets(s, symbol):
    import yfinance as yf

    def call():
        info = yf.Ticker(symbol).info or {}
        v = info.get("totalAssets")
        if not v:
            raise SourceUnavailable("yahoo", "empty", f"totalAssets for {symbol}")
        return {"symbol": symbol, "total_assets": float(v)}
    d, pid = s.fetch("yahoo", {"fn": "totalAssets", "symbol": symbol}, call, kind="fund_info", fmt="json",
                     capture=lambda r: (json.dumps(r).encode(), None, None))
    return d["total_assets"]


# ---------------------------------------------------------------- Diesel & Refining sources (public, no key)
def last_eia_release(now=None):
    """Most recent scheduled WPSR publication (Wednesday 10:30 ET; allow until 11:00 for file updates)."""
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    now = (now or datetime.now(timezone.utc)).astimezone(et)
    d = now - timedelta(days=(now.weekday() - 2) % 7)
    rel = d.replace(hour=11, minute=0, second=0, microsecond=0)
    if rel > now:
        rel -= timedelta(days=7)
    return rel.astimezone(timezone.utc)


def _cached_eia(s, series_id):
    """Reuse a previously captured EIA file when no newer weekly release can exist since it was fetched."""
    with lake._WRITE_LOCK:
        row = s.conn.execute("""SELECT payload_id, fetched_at FROM v2_payloads WHERE kind = 'eia_weekly'
                                AND request_json LIKE ? ORDER BY fetched_at DESC LIMIT 1""", (f'%"series": "{series_id}"%',)).fetchone()
    if not row or not row["fetched_at"]:
        return None
    fetched = datetime.fromisoformat(row["fetched_at"])
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    if fetched < last_eia_release():
        return None
    with lake._WRITE_LOCK:
        _, content = lake.load_payload(s.conn, row["payload_id"])
    if not content or not (content.startswith(b"\xd0\xcf\x11\xe0") or content[:1] == b"{"):
        return None
    s._count(s.cache_hits, "eia")
    return content, row["payload_id"]


EIA_API_LENGTH = 5000      # the API's row limit; weekly series start in 1982 (~2,300 rows), so this is the full history


def _eia_truncated(content):
    """True for an API capture that holds fewer rows than the series has (older runs asked for 700 weeks)."""
    if content[:1] != b"{":
        return False
    resp = json.loads(content).get("response", {})
    try:
        return int(resp.get("total") or 0) > len(resp.get("data") or [])
    except (TypeError, ValueError):
        return False


def _eia_parse(content):
    """EIA series content -> Series. Accepts the API v2 JSON or the public dnav .xls history file."""
    if content[:1] == b"{":
        rows = json.loads(content)["response"]["data"]
        ser = pd.Series({pd.Timestamp(r["period"]): float(r["value"]) for r in rows if r.get("value") is not None})
        return ser.sort_index()
    df = pd.read_excel(io.BytesIO(content), sheet_name="Data 1", skiprows=2, engine="xlrd")
    return pd.Series(pd.to_numeric(df.iloc[:, 1], errors="coerce").values, index=pd.to_datetime(df.iloc[:, 0])).dropna().sort_index()


def eia_weekly(s, series_id):
    """EIA weekly series, full history: API v2 when EIA_API_KEY is set, else the public dnav .xls history file.
    Data changes once a week, so a capture newer than the last scheduled release is reused (no request)."""
    import requests
    from config import EIA_API_KEY
    cached = _cached_eia(s, series_id)
    if cached and not _eia_truncated(cached[0]):
        content, pid = cached
        return _eia_parse(content), pid
    if EIA_API_KEY:
        def api_call():
            r = requests.get(f"https://api.eia.gov/v2/seriesid/PET.{series_id}.W", timeout=40,
                             params={"api_key": EIA_API_KEY, "length": EIA_API_LENGTH})
            if r.status_code != 200 or not r.content.startswith(b"{"):
                raise SourceUnavailable("eia", f"http_{r.status_code}", series_id)
            if not json.loads(r.content).get("response", {}).get("data"):
                raise SourceUnavailable("eia", "empty", series_id)
            return r.content
        try:
            content, pid = s.fetch("eia", {"series": series_id, "freq": "weekly", "via": "api_v2"}, api_call,
                                   kind="eia_weekly", fmt="json", capture=lambda c: (c, None, None))
            return _eia_parse(content), pid
        except SourceUnavailable:
            pass                                    # fall back to the public history file

    def call():
        r = requests.get(f"https://www.eia.gov/dnav/pet/hist_xls/{series_id}w.xls", timeout=40,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200 or not r.content.startswith(b"\xd0\xcf\x11\xe0"):
            raise SourceUnavailable("eia", f"http_{r.status_code}", series_id)
        return r.content
    content, pid = s.fetch("eia", {"series": series_id, "freq": "weekly"}, call, kind="eia_weekly", fmt="xls",
                           capture=lambda c: (c, None, None))
    return _eia_parse(content), pid


def eia_refinery_capacity(s):
    """Operable atmospheric crude capacity (barrels per calendar day) per refinery, EIA Refinery Capacity Report."""
    import requests
    from datetime import date as _d
    with lake._WRITE_LOCK:
        row = s.conn.execute("""SELECT payload_id, fetched_at, coverage_json FROM v2_payloads WHERE kind = 'refinery_capacity'
                                ORDER BY fetched_at DESC LIMIT 1""").fetchone()
    if row and row["fetched_at"]:
        f = datetime.fromisoformat(row["fetched_at"])
        f = f if f.tzinfo else f.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - f < timedelta(days=30):      # annual report: monthly refresh is plenty
            with lake._WRITE_LOCK:
                _, content = lake.load_payload(s.conn, row["payload_id"])
            if content and content.startswith(b"PK"):
                s._count(s.cache_hits, "eia")
                year = json.loads(row["coverage_json"] or "{}").get("year")
                d = pd.read_excel(io.BytesIO(content))
                c = d[(d["PRODUCT"] == "TOTAL OPERABLE CAPACITY") & d["SUPPLY"].str.contains("calendar day", case=False, na=False)]
                t = c.groupby(["COMPANY_NAME", "SITE", "STATE_NAME", "PADD"], as_index=False)["QUANTITY"].sum()
                return t.rename(columns={"QUANTITY": "capacity_bpd"}), year, row["payload_id"]

    def call():
        last = None
        for yy in (_d.today().year % 100, _d.today().year % 100 - 1):
            r = requests.get(f"https://www.eia.gov/petroleum/refinerycapacity/refcap{yy:02d}.xlsx", timeout=60,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200 and r.content.startswith(b"PK"):
                return {"year": 2000 + yy, "content": r.content}
            last = r.status_code
        raise SourceUnavailable("eia", f"http_{last}", "refinery capacity report")
    res, pid = s.fetch("eia", {"file": "refcap"}, call, kind="refinery_capacity", fmt="xlsx",
                       capture=lambda r: (r["content"], None, {"year": r["year"]}))
    d = pd.read_excel(io.BytesIO(res["content"]))
    c = d[(d["PRODUCT"] == "TOTAL OPERABLE CAPACITY") & d["SUPPLY"].str.contains("calendar day", case=False, na=False)]
    t = c.groupby(["COMPANY_NAME", "SITE", "STATE_NAME", "PADD"], as_index=False)["QUANTITY"].sum()
    return t.rename(columns={"QUANTITY": "capacity_bpd"}), res["year"], pid


def tceq_feed(s):
    """TCEQ Air Emission Event Reports RSS (hourly refreshed, ~5 days of events)."""
    import requests

    def call():
        r = requests.get("https://www2.tceq.texas.gov/oce/eer/RSSDataFeed.xml", timeout=30,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        ET.fromstring(r.content)
        return r.content
    content, pid = s.fetch("tceq", {"feed": "RSSDataFeed"}, call, kind="tceq_rss", fmt="xml",
                           capture=lambda c: (c, None, None))
    items = []
    for it in ET.fromstring(content).findall(".//item"):
        title = (it.findtext("title") or "").strip()
        m = __import__("re").match(r"(\d+)\s*-\s*(RN\d+)\s+(.*)", title)
        items.append({"event_id": m.group(1) if m else None, "rn": m.group(2) if m else None,
                      "name": (m.group(3) if m else title).strip(), "link": it.findtext("link"),
                      "published": it.findtext("pubDate")})
    return items, pid


def tceq_event(s, event_id):
    """Parse one TCEQ event detail page into structured fields."""
    import html as _html
    import re
    import requests

    def call():
        r = requests.get("https://www2.tceq.texas.gov/oce/eer/index.cfm", timeout=30,
                         params={"fuseaction": "main.getDetails", "target": event_id}, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.text
    text, pid = s.fetch("tceq", {"event": event_id}, call, kind="tceq_event", fmt="html",
                        capture=lambda t: (t.encode(), None, None))
    t = _html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "|", text)))
    t = re.sub(r"(\|\s*)+", "|", t)

    def between(a, b):
        i = t.find(a)
        if i < 0:
            return None
        j = t.find(b, i + len(a)) if b else -1
        v = t[i + len(a): j if j > 0 else i + len(a) + 600]
        return v.strip(" |") or None
    units = between("Process Unit or Area Common Names", "List of Air Contaminant")
    return {"event_id": str(event_id), "name": between("Regulated Entity Name:", "RN:"), "rn": between("RN:", "Physical Location"),
            "location": between("Physical Location:", "County:"), "county": between("County:", "Event/Activity Type"),
            "event_type": between("Event/Activity Type:", "Date and Time"),
            "start": between("Date and Time Event Discovered or Scheduled Activity Start:", "Date and Time Event or Scheduled"),
            "end": between("Date and Time Event or Scheduled Activity Ended:", "Event Duration"),
            "duration": between("Event Duration:", "Process Unit"), "units": units,
            "cause": between("Reason for Scheduled Activity:", "Actions Taken"),
            "notified": between("Initial Notification Date/Time:", "Method:"), "payload_id": pid,
            "url": f"https://www2.tceq.texas.gov/oce/eer/index.cfm?fuseaction=main.getDetails&target={event_id}"}
