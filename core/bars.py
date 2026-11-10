"""The CHART tab's store: price bars and non-price series, kept apart from portfolio.db in config.CHART_STORE.

chart.db is its own lake (lake.migrate, so every chart fetch is captured in v2_payloads) plus v2_bars (1d and 1wk
rows, INSERT OR IGNORE only), v2_chart_symbols and v2_chart_fans (core/chartfans.py). Intraday bars (1m, 5m, 1h) are Parquet
day files,
bars/<SYMBOL>/<tf>/<YYYY-MM-DD>.parquet (New York date), written once per finished session and never rewritten.
Everything else (15m, 30m, 4h, 1mo) is resampled on read. Network goes through SourceSession.fetch().
Daily and weekly ts is the exchange-local date (YYYY-MM-DD); intraday ts is the bar open in UTC.
"""
import fcntl
import io
import json
import re
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

import config
from core import lake, sources

NY = ZoneInfo("America/New_York")
SYMBOL_RE = re.compile(r"^[A-Z0-9.^=\-]{1,15}$")
NATIVE = ("1m", "5m", "1h", "1d", "1wk")
INTRADAY = ("1m", "5m", "1h")
# derived tf -> (rule, stored timeframes to build it from, finest first)
DERIVED = {"15m": ("15min", ("5m", "1m")), "30m": ("30min", ("5m", "1m")), "4h": ("4h", ("1h",)), "1mo": ("MS", ("1d",))}
TF_MINUTES = {"1m": 1, "5m": 5, "1h": 60, "15m": 15, "30m": 30, "4h": 240}
COLS = ["open", "high", "low", "close", "volume"]
DESCRIPTIONS = {
    "^VIX": "CBOE Volatility Index: the market's 30-day implied volatility of S&P 500 options, the 'fear gauge'.",
    "^GSPC": "S&P 500 index: the 500 largest US companies, weighted by market value.",
    "^TNX": "CBOE 10-year Treasury yield index (the yield in percent).",
    "SI=F": "COMEX silver futures, front month (USD per troy ounce).",
    "GC=F": "COMEX gold futures, front month (USD per troy ounce).",
    "CL=F": "NYMEX WTI crude oil futures, front month (USD per barrel).",
    "HO=F": "NYMEX ULSD (heating oil / diesel) futures, front month (USD per gallon).",
    "DX-Y.NYB": "US Dollar Index: the dollar against a basket of six major currencies.",
    "BTC-USD": "Bitcoin priced in US dollars.",
    "SPY": "SPDR S&P 500 ETF: tracks the S&P 500, the most traded ETF.",
    "SLV": "iShares Silver Trust: an ETF that holds physical silver.",
    "QQQ": "Invesco QQQ: an ETF tracking the Nasdaq-100 (large US tech and growth companies).",
    "GLD": "SPDR Gold Shares: an ETF that holds physical gold.",
}


# ---------------------------------------------------------------- basics
def now_ny():
    return datetime.now(NY)


def norm_symbol(symbol):
    """Upper-cased symbol, or ValueError. Also refuses names that start with '.', which would climb out of bars/."""
    sym = str(symbol or "").strip().upper()
    if not SYMBOL_RE.match(sym) or sym.startswith("."):
        raise ValueError(f"bad symbol {symbol!r}")
    return sym


def _folder(symbol):
    return symbol.replace("^", "_")          # reversible: '_' is not an allowed symbol character


def _day_dir(symbol, tf):
    return config.CHART_STORE / "bars" / _folder(norm_symbol(symbol)) / tf


AROUND_THE_CLOCK = re.compile(r"(=F|=X|-USD)$|^DX-Y")      # futures, FX and crypto trade past 20:00 ET


def finished(day, now=None, symbol=""):
    """True when the New York session of `day` is over: an earlier date, or today after 20:00 (end of extended hours).
    Futures, FX and crypto keep trading into the night, so their `day` is over only once the New York date has turned."""
    now = now or now_ny()
    return day < now.date() or (day == now.date() and now.hour >= 20 and not AROUND_THE_CLOCK.search(symbol))


@contextmanager
def job_lock():
    """One bars job at a time (waits for the other to finish)."""
    config.CHART_STORE.mkdir(parents=True, exist_ok=True)
    with open(config.CHART_STORE / ".bars.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def connect():
    config.CHART_STORE.mkdir(parents=True, exist_ok=True)
    conn = lake.connect(config.CHART_STORE / "chart.db")
    lake.migrate(conn)
    with conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS v2_bars (
            symbol TEXT NOT NULL, tf TEXT NOT NULL, ts TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL,
            volume REAL, source TEXT NOT NULL, payload_id INTEGER, fetched_at TEXT,
            PRIMARY KEY (symbol, tf, ts)) WITHOUT ROWID""")
        conn.execute("""CREATE TABLE IF NOT EXISTS v2_chart_symbols (
            symbol TEXT PRIMARY KEY, name TEXT, kind TEXT, exchange TEXT, description TEXT, source TEXT, added_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS v2_chart_fans (
            item TEXT NOT NULL, tf TEXT NOT NULL, asof TEXT NOT NULL, model TEXT NOT NULL, version TEXT, built_at TEXT,
            json TEXT, PRIMARY KEY (item, tf, asof, model))""")
    return conn


def connect_readonly():
    """chart.db for the terminal's routes, or None before the first bars job has created it."""
    try:
        return lake.connect_readonly(config.CHART_STORE / "chart.db")
    except Exception:
        return None


def _intraday_symbols():
    root = config.CHART_STORE / "bars"
    return {d.name.replace("_", "^") for d in root.iterdir() if d.is_dir()} if root.exists() else set()


def tracked(conn):
    """Symbols with daily bars or intraday day files (Yahoo has no daily history for some, e.g. 10Y=F)."""
    return sorted({r[0] for r in conn.execute("SELECT DISTINCT symbol FROM v2_bars")} | _intraday_symbols())


def has(conn, symbol):
    return bool(conn.execute("SELECT 1 FROM v2_bars WHERE symbol = ? LIMIT 1", (symbol,)).fetchone()) or \
        _day_dir(symbol, "1h").exists() or _day_dir(symbol, "5m").exists()


# ---------------------------------------------------------------- normalise and store
def _norm(df, tf):
    """Provider frame (Open/High/Low/Close/Volume, any index) -> lower-case OHLCV with a UTC index (daily and weekly:
    the local date at midnight UTC). Rows without a close are dropped."""
    df = df.rename(columns=str.lower)
    df = df[[c for c in COLS if c in df.columns]].apply(pd.to_numeric, errors="coerce")
    df = df.reindex(columns=COLS)
    idx = df.index
    if tf in ("1d", "1wk"):
        idx = pd.DatetimeIndex(pd.to_datetime([str(x)[:10] for x in idx])).tz_localize("UTC")
    else:
        idx = pd.DatetimeIndex(pd.to_datetime(idx, utc=True))
    df.index = idx
    df.index.name = "ts"
    df = df[df["close"].notna()]
    return df[~df.index.duplicated(keep="first")].sort_index()


def _drop_partial_first(df, tf):
    """A period fetch can start mid-session: drop its oldest NY date (an earlier run or fetch stored that day whole)."""
    if tf in ("1d", "1wk") or df.empty:
        return df
    days = df.index.tz_convert(NY).date
    return df[days != days.min()] if len(set(days)) > 1 else df


def store_daily(conn, symbol, tf, df, source, payload_id=None, now=None):
    """INSERT OR IGNORE finished 1d/1wk rows into v2_bars; returns the rows added."""
    now = now or now_ny()
    today = now.date()
    rows, stamp = [], lake.utc_now_iso()
    for ts, r in df.iterrows():
        d = ts.date()
        if (tf == "1d" and not finished(d, now, symbol)) or (tf == "1wk" and d + timedelta(days=4) >= today):
            continue
        rows.append((symbol, tf, d.isoformat(), *[None if pd.isna(r[c]) else float(r[c]) for c in COLS], source,
                     payload_id, stamp))
    with conn:
        before = conn.total_changes
        conn.executemany("INSERT OR IGNORE INTO v2_bars VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        return conn.total_changes - before


def write_days(symbol, tf, df, now=None):
    """Write each finished New York session in `df` as a day file, unless the file exists. Returns files written."""
    now = now or now_ny()
    out = _day_dir(symbol, tf)
    n = 0
    for day, g in df.groupby(df.index.tz_convert(NY).date):
        path = out / f"{day.isoformat()}.parquet"
        if path.exists() or not finished(day, now, symbol):
            continue
        out.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        g = pd.DataFrame({"ts": g.index.tz_convert("UTC").tz_localize(None),
                          **{c: g[c].astype("float32") for c in ("open", "high", "low", "close")},
                          "volume": g["volume"].fillna(0).astype("int64")})
        g.to_parquet(tmp, compression="zstd", index=False)
        tmp.replace(path)
        n += 1
    return n


def ingest(conn, symbol, tf, frame, source, payload_id=None, now=None, partial_first=False):
    """Store a provider frame as v2_bars rows (1d, 1wk) or day files (1m, 5m, 1h). Returns rows or files added."""
    df = _norm(frame, tf)
    if partial_first:
        df = _drop_partial_first(df, tf)
    if tf in INTRADAY:
        return write_days(symbol, tf, df, now)
    return store_daily(conn, symbol, tf, df, source, payload_id, now)


# ---------------------------------------------------------------- read
def _read_files(symbol, tf, start, end, want):
    d = _day_dir(symbol, tf)
    if not d.exists():
        return pd.DataFrame(columns=COLS)
    lo = (start.tz_convert(NY).date() - timedelta(days=1)).isoformat() if start is not None else ""
    hi = (end.tz_convert(NY).date() + timedelta(days=1)).isoformat() if end is not None else "9999"
    files = sorted(p for p in d.glob("*.parquet") if lo <= p.stem <= hi)
    parts, n = [], 0
    for p in reversed(files):                       # newest first, stop once there are enough bars
        g = pd.read_parquet(p)
        parts.append(g)
        n += len(g)
        if n >= want:
            break
    if not parts:
        return pd.DataFrame(columns=COLS)
    df = pd.concat(parts[::-1])
    df.index = pd.DatetimeIndex(df.pop("ts")).tz_localize("UTC")
    return df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})


def _read_daily(conn, symbol, tf, start, end):
    q, args = "SELECT ts, open, high, low, close, volume FROM v2_bars WHERE symbol = ? AND tf = ?", [symbol, tf]
    if start is not None:
        q, args = q + " AND ts >= ?", args + [start.date().isoformat()]
    if end is not None:
        q, args = q + " AND ts <= ?", args + [end.date().isoformat()]
    rows = conn.execute(q + " ORDER BY ts", args).fetchall()
    df = pd.DataFrame([tuple(r) for r in rows], columns=["ts"] + COLS)
    df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("ts"))).tz_localize("UTC")
    return df


def _resample(df, rule):
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    if rule == "MS":
        out = df.resample("MS", label="left", closed="left").agg(agg)
    else:                                            # per New York day, so a bucket never spans the overnight gap
        local = df.tz_convert(NY)
        out = pd.concat([g.resample(rule, label="left", closed="left", origin="start_day").agg(agg)
                         for _, g in local.groupby(local.index.date)]).tz_convert("UTC")
    return out.dropna(subset=["open"])


def _utc(x):
    if x is None:
        return None
    x = pd.Timestamp(x)
    return x.tz_localize("UTC") if x.tzinfo is None else x.tz_convert("UTC")


def read_bars(conn, symbol, tf, start=None, end=None, limit=20000):
    """OHLCV frame (UTC index, lower-case columns), the newest `limit` bars in [start, end]. tf: 1m 5m 15m 30m 1h 4h 1d 1wk 1mo."""
    symbol = norm_symbol(symbol)
    if tf not in NATIVE and tf not in DERIVED:
        raise ValueError(f"bad timeframe {tf!r}")
    start, end = _utc(start), _utc(end)
    if end is not None and end == end.normalize():     # a bare date means through the end of that day
        end += pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    rule, sources_ = DERIVED.get(tf, (None, (tf,)))
    for src in sources_:
        ratio = max(1, TF_MINUTES.get(tf, 1) // TF_MINUTES.get(src, 1)) if src in INTRADAY else 1
        if src in INTRADAY:
            df = _read_files(symbol, src, start, end, limit * ratio)
        else:
            df = _read_daily(conn, symbol, src, start, end)
        if not df.empty:
            break
    if rule and not df.empty:
        df = _resample(df, rule)
    if df.empty and tf in ("1d", "1wk", "1mo"):            # no daily history stored: build it from the intraday files
        for src in ("1h", "5m"):
            df = _read_files(symbol, src, start, end, 10 ** 9)
            if not df.empty:
                break
        if not df.empty:
            df = _resample(df, "1D")
            df.index = pd.DatetimeIndex(df.index.tz_convert(NY).date).tz_localize("UTC")   # like stored daily bars
            if tf != "1d":
                df = df.resample("W-MON" if tf == "1wk" else "MS", label="left", closed="left").agg(
                    {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["open"])
    if start is not None:
        df = df[df.index >= start]
    if end is not None:
        df = df[df.index <= end]
    return df.tail(limit)


# ---------------------------------------------------------------- symbols
def _trim(text, n=220):
    sents = re.split(r"(?<=[.!?])\s+", " ".join(str(text).split()))
    out = sents[0]
    if len(sents) > 1 and len(out) + 1 + len(sents[1]) <= n:
        out += " " + sents[1]
    return out if len(out) <= n else out[:n - 1].rstrip() + "…"


def ensure_symbol(conn, s, symbol):
    """First time a symbol is seen: record its name, kind, exchange and a short description (INSERT OR IGNORE)."""
    if conn.execute("SELECT 1 FROM v2_chart_symbols WHERE symbol = ?", (symbol,)).fetchone():
        return
    info, source = {}, "yahoo"

    def call():
        import yfinance as yf
        i = yf.Ticker(symbol).info or {}
        return {k: i.get(k) for k in ("longName", "shortName", "quoteType", "exchange", "fullExchangeName", "longBusinessSummary")}
    try:
        info, _ = s.fetch("yahoo", {"fn": "info", "symbol": symbol}, call, kind="symbol_info", fmt="json",
                          capture=lambda r: (json.dumps(r).encode(), None, None))
    except sources.SourceUnavailable:
        source = "none"
    name = info.get("longName") or info.get("shortName") or symbol
    kind = info.get("quoteType") or ""
    desc = _trim(info["longBusinessSummary"]) if info.get("longBusinessSummary") else DESCRIPTIONS.get(symbol) or f"{name} · {kind}"
    with conn:
        conn.execute("INSERT OR IGNORE INTO v2_chart_symbols VALUES (?,?,?,?,?,?,?)",
                     (symbol, name, kind, info.get("fullExchangeName") or info.get("exchange") or "", desc, source,
                      lake.utc_now_iso()))


def catalog_symbols(conn):
    meta = {r["symbol"]: r for r in conn.execute("SELECT * FROM v2_chart_symbols")}
    out = []
    for r in conn.execute("SELECT symbol, MIN(ts) a, MAX(ts) b FROM v2_bars WHERE tf = '1d' GROUP BY symbol ORDER BY symbol"):
        m = meta.get(r["symbol"])
        out.append({"symbol": r["symbol"], "name": m["name"] if m else r["symbol"], "kind": m["kind"] if m else "",
                    "exchange": m["exchange"] if m else "",
                    "description": (m["description"] if m else None) or DESCRIPTIONS.get(r["symbol"], ""),
                    "first": r["a"], "last": r["b"]})
    return out


# ---------------------------------------------------------------- fetching (Yahoo, Alpaca)
NORMAL = (("1d", {"period": "7d"}), ("1wk", {"period": "1mo"}), ("1m", {"period": "7d"}), ("5m", {"period": "7d"}), ("1h", {"period": "7d"}))
BACKFILL = (("1d", {"period": "max"}), ("1wk", {"period": "max"}), ("1h", {"period": "730d"}), ("5m", {"period": "60d"}))


def _fetch_yahoo(conn, s, symbol, tf, kw, now=None):
    df, pid = sources.yf_history(s, symbol, interval=tf, prepost=tf in INTRADAY, **kw)
    return ingest(conn, symbol, tf, df, "yahoo", pid, now, partial_first="period" in kw and kw["period"] not in ("max",))


def _deep(conn, s, symbol, now=None):
    """Alpaca 1m since 2016 in 31-day chunks, a day file per session. A chunk that already has 15+ day files is skipped,
    so a killed run resumes (ponytail: a chunk half-filled by Yahoo is skipped too; delete its files to refill)."""
    now, errors, got = now or now_ny(), [], 0
    have = {p.stem for p in _day_dir(symbol, "1m").glob("*.parquet")} if _day_dir(symbol, "1m").exists() else set()
    d, last = date(2016, 1, 4), now.date() - timedelta(days=1)
    while d <= last:
        e = min(d + timedelta(days=30), last)
        if sum(1 for k in range((e - d).days + 1) if (d + timedelta(days=k)).isoformat() in have) < 15:
            try:
                # New York midnights, not bare dates (Alpaca reads those as 00:00 UTC and cuts the last evening off)
                df, _ = sources.alpaca_bars(s, config.ALPACA_API_KEY_ID, config.ALPACA_API_SECRET_KEY, symbol,
                                            datetime.combine(d, time(), NY).isoformat(),
                                            datetime.combine(e + timedelta(days=1), time(), NY).isoformat())
                got += ingest(conn, symbol, "1m", df, "alpaca", None, now)
            except sources.SourceUnavailable as exc:
                errors.append(f"alpaca {symbol} {d}: {exc}")
                if exc.outcome in ("not_configured", "offline", "http_401", "http_403"):
                    break
        d = e + timedelta(days=1)
    return got, errors


def collect(conn, s, symbols, backfill=False, deep=False, now=None):
    """Fetch and store bars for `symbols`. Returns {"added": n, "errors": [...]}; nothing is raised for a provider failure."""
    added, errors = 0, []
    for symbol in symbols:
        symbol = norm_symbol(symbol)
        ensure_symbol(conn, s, symbol)
        plan = list(BACKFILL if backfill else NORMAL)
        if backfill:                                  # 1m: Yahoo refuses a window of 8+ days; about 30 days back
            today = (now or now_ny()).date()          # end is exclusive, so each window is 7 days and today is left out
            plan += [("1m", {"start": (today - timedelta(days=7 * (k + 1))).isoformat(),
                             "end": (today - timedelta(days=7 * k)).isoformat()}) for k in range(4)]
        for tf, kw in plan:
            try:
                added += _fetch_yahoo(conn, s, symbol, tf, kw, now)
            except sources.SourceUnavailable as exc:
                errors.append(f"yahoo {symbol} {tf}: {exc}")
        if deep:
            n, errs = _deep(conn, s, symbol, now)
            added, errors = added + n, errors + errs
    return {"added": added, "errors": errors}


# ---------------------------------------------------------------- from what the lake already captured
_LAKE_TF = {"1d": "1d", "1wk": "1wk", "1h": "1h", "60m": "1h", "5m": "5m", "1m": "1m"}


def from_lake(conn, main_db=None, cache_dir=None, now=None):
    """Free backfill: every stored `ohlcv` payload of portfolio.db (read-only, newest capture first so it wins) and
    CME_Data/cache/bars_*.csv (daily, no volume). Returns {"payloads": n, "added": n, "skipped": n}."""
    main_db = main_db or config.DB_PATH
    cache_dir = cache_dir or config.DATA_DIR / "cache"
    res = {"payloads": 0, "added": 0, "skipped": 0}
    if main_db.exists():
        src = lake.connect_readonly(main_db)
        try:
            for r in src.execute("""SELECT payload_id, request_json, fetched_at FROM v2_payloads WHERE kind = 'ohlcv'
                                    ORDER BY payload_id DESC""").fetchall():
                req = json.loads(r["request_json"] or "{}")
                # daily only: the pipeline's intraday captures are regular-hours and Yahoo's backfill has longer ones with
                # pre/post market; a capture's own day counts as finished only if it was fetched after the session
                tf = _LAKE_TF.get(req.get("interval", ""))
                tf = tf if tf in ("1d", "1wk") else None
                at = pd.Timestamp(r["fetched_at"]) if r["fetched_at"] else None
                at = None if at is None else (at.tz_localize("UTC") if at.tzinfo is None else at).tz_convert(NY)
                try:
                    sym = norm_symbol(req.get("symbol"))
                    _, content = lake.load_payload(src, r["payload_id"])
                    if tf is None or not content:
                        raise ValueError("unusable")
                    df = pd.read_csv(io.BytesIO(content), index_col=0)
                    res["added"] += ingest(conn, sym, tf, df, "lake", r["payload_id"], min(now or at, at) if at is not None else now)
                    res["payloads"] += 1
                except (ValueError, KeyError, pd.errors.ParserError):
                    res["skipped"] += 1
        finally:
            src.close()
    for p in sorted(cache_dir.glob("bars_*.csv")) if cache_dir.exists() else []:
        try:
            sym = norm_symbol(p.stem[5:].rsplit("_", 1)[0])
            res["added"] += ingest(conn, sym, "1d", pd.read_csv(p, index_col=0), "lake", None, now)
            res["payloads"] += 1
        except (ValueError, KeyError, pd.errors.ParserError):
            res["skipped"] += 1
    return res


# ---------------------------------------------------------------- non-price series (stored data, read from portfolio.db)
def _obs(metric, entity="", key=None):
    def load(conn):
        rows = conn.execute("""SELECT observed_at, value_num, value_json FROM v2_observations WHERE metric_id = ? AND entity = ?
                               AND status != 'missing' ORDER BY observed_at, obs_id""", (metric, entity)).fetchall()
        vals = {}
        for r in rows:
            v = r["value_num"] if key is None else (json.loads(r["value_json"] or "{}").get(key))
            if v is not None:
                vals[pd.Timestamp(str(r["observed_at"])[:10])] = float(v)
        return pd.Series(vals, dtype=float).sort_index()
    return load


_EIA_CACHE = {}


def _eia(conn, sid):
    row = conn.execute("""SELECT payload_id FROM v2_payloads WHERE kind = 'eia_weekly' AND request_json LIKE ?
                          ORDER BY fetched_at DESC LIMIT 1""", (f'%"series": "{sid}"%',)).fetchone()
    if not row:
        return pd.Series(dtype=float)
    if row["payload_id"] not in _EIA_CACHE:       # parsing the .xls history is slow; payloads never change
        if len(_EIA_CACHE) > 40:                  # superseded weekly payloads would pile up for the life of the server
            _EIA_CACHE.clear()
        content = lake.load_payload(conn, row["payload_id"])[1]
        _EIA_CACHE[row["payload_id"]] = sources._eia_parse(content) if content else pd.Series(dtype=float)
    return _EIA_CACHE[row["payload_id"]]


def _days(stock_id, supp_id):
    def load(conn):
        st, su = _eia(conn, stock_id), _eia(conn, supp_id)
        f = pd.DataFrame({"stocks": st, "supplied": su.rolling(4).mean()}).dropna()
        f = f[f["supplied"] > 0]
        return f["stocks"] / f["supplied"]
    return load


def _series_catalog():
    from core import fans
    out = {f"eia.{k.split('.')[1]}": (label, unit, "W", f"EIA weekly US {label.lower()}, thousand barrels.", lambda c, i=i: _eia(c, i))
           for k, (label, unit, i) in fans.WEEKLY.items() if i in ("WDISTUS1", "WGTSTUS1")}
    for k, (label, stock_id, supp_id) in fans.DAYS.items():
        out[f"eia.{k}"] = (label.replace("Distillate", "Diesel (distillate)") if "distillate" in k else label, "days", "W",
                           f"How many days the stock would last at the last 4 weeks' average product supplied (EIA {stock_id} / {supp_id}).",
                           _days(stock_id, supp_id))
    out["chaos.score"] = ("Chaos score", "index", "D", "Our composite stress index of market, inventory and political disorder; higher is more chaotic.", _obs("chaos.score"))
    out["fedops.repo_day:rrp"] = ("Fed reverse repo (RRP)", "USD bn", "D", "Cash parked at the Fed overnight by money funds; falls when liquidity returns to markets.",
                                  _obs("fedops.repo_day", "rrp", "accepted_bn"))
    out["fedops.ref_rate:SOFR"] = ("SOFR", "%", "D", "Secured Overnight Financing Rate: what overnight loans against Treasuries cost.", _obs("fedops.ref_rate", "SOFR"))
    out["macro.vmri.score"] = ("VMRI score", "index", "per run", "Our macro risk index: dollar x 10y yield, credit spread and VIX combined.", _obs("macro.vmri.score"))
    return out


SERIES = _series_catalog()


def read_series(conn, sid, start=None, end=None):
    """(t, v) lists for a catalog series from the read-only portfolio.db `conn`; t is unix seconds."""
    s = SERIES[sid][4](conn)
    if s.empty:                                  # an empty series has no date index to compare with
        return [], []
    if start is not None:
        s = s[s.index >= pd.Timestamp(start).tz_localize(None)]
    if end is not None:
        s = s[s.index <= pd.Timestamp(end).tz_localize(None)]
    return [int(pd.Timestamp(t).tz_localize("UTC").timestamp()) for t in s.index], [round(float(v), 6) for v in s.values]


def catalog_series(conn):
    out = []
    for sid, (label, unit, freq, desc, loader) in SERIES.items():
        try:
            s = loader(conn) if conn is not None else pd.Series(dtype=float)
        except Exception:                      # a missing table or payload: the series is listed without dates
            s = pd.Series(dtype=float)
        out.append({"id": sid, "label": label, "unit": unit, "freq": freq, "description": desc,
                    "first": s.index[0].date().isoformat() if len(s) else None, "last": s.index[-1].date().isoformat() if len(s) else None})
    return out
