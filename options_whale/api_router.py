import functools
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import warnings
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlparse
from xml.dom import minidom
from xml.sax.saxutils import escape as xml_escape

import databento as db
import numpy as np
import pandas as pd
import requests
import yfinance as yf
from flask import Flask, jsonify, render_template_string, Response, request, render_template
from werkzeug.exceptions import HTTPException
from werkzeug.serving import WSGIRequestHandler

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import config
from config import DATA_DIR as _DATA_DIR, DB_PATH as _V2_DB_PATH, PROJECT_ROOT as _PROJECT_ROOT
from core.chaos import CHAOS_VERSION
from core import forecast as _v2fc, lake as _v2lake, metrics, positions as _v2pos, runlock
from quant_engine import QuantEngine


PYTHON_BIN = sys.executable   # subprocess routes use the server's own interpreter (the project .venv)
RUN_COMMAND = str(_PROJECT_ROOT / "run_dashboard.command")
EBAY_SCRIPT_PATH = str(_PROJECT_ROOT / "ebay.py")

DATA_DIR = str(_DATA_DIR)
INVENTORY_CSV = os.path.join(DATA_DIR, "comex_inventory_history.csv")
LEDGER_CSV = os.path.join(DATA_DIR, "macro_master_ledger.csv")
INSTITUTIONAL_LEDGER_CSV = os.path.join(DATA_DIR, "equities_darkpool_gex_ledger.csv")
MANUAL_RUN_LOG = os.path.join(DATA_DIR, ".v2_manual_run.log")        # output of runs started by POST /run

quant = QuantEngine(LEDGER_CSV)

# Suppress pandas FutureWarnings for clean terminal output
warnings.simplefilter(action='ignore', category=FutureWarning)

# ==========================================
# IN-MEMORY CACHE MANAGER
# ==========================================
class DataCache:
    def __init__(self, ttl_seconds=60):
        self.ttl = ttl_seconds
        self.cache = {}
        self.last_update = {}

    def get_csv(self, file_path):
        current_time = time.time()
        if file_path in self.cache:
            if current_time - self.last_update.get(file_path, 0) < self.ttl:
                return self.cache[file_path]
        if not os.path.exists(file_path):
            return pd.DataFrame()
        try:
            df = pd.read_csv(file_path)
            self.cache[file_path] = df
            self.last_update[file_path] = current_time
            return df
        except Exception as e:
            print(f"Cache load error for {file_path}: {e}")
            return pd.DataFrame()

    def get(self, key):
        current_time = time.time()
        if key in self.cache:
            if current_time - self.last_update.get(key, 0) < self.ttl:
                return self.cache[key]
        return None

    def set(self, key, value):
        self.cache[key] = value
        self.last_update[key] = time.time()

data_cache = DataCache(ttl_seconds=60)

app = Flask(__name__)

# 1. Force HTML templates to reload instantly (disable to make faster)
app.config['TEMPLATES_AUTO_RELOAD'] = True

# 2. Force CSS/JS to never cache (Bulletproof method, disable to make faster)
@app.after_request
def add_header(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# No CORS: the terminal is served by this app, so its pages only ever call it from the same origin, and other
# sites must not be able to read the data.


@app.before_request
def reject_cross_site_writes():
    """The server has no login, so a page on any other site must not be able to make a browser send it a
    state-changing request (start a run, scan eBay, star/stop a position). Browsers attach Origin (or at least
    Referer) to such requests; refuse when it names a different host than the one being served. Every route
    that writes or starts a process is POST or DELETE, so this covers them all."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if source and urlparse(source).netloc != request.host:
        return jsonify({"status": "error", "message": "cross-site request refused"}), 403
    return None


# ==========================================
# ERRORS AND REQUEST PARAMETERS
# ==========================================
# Failures answer with a real 4xx/5xx status and a short message. Exception text, tracebacks and subprocess output
# go to the server log (stderr), never to the client.
def json_error(message, status):
    return jsonify({"status": "error", "message": message}), status


def xml_error(message, status):
    return Response(f"<error>{xml_escape(message)}</error>", mimetype='application/xml', status=status)


class ApiError(Exception):
    """Raised anywhere in a request to answer with a short error: JSON by default, `<error>` XML for XML routes."""
    def __init__(self, message, status=400, xml=False):
        super().__init__(message)
        self.message, self.status, self.xml = message, status, xml


@app.errorhandler(ApiError)
def _handle_api_error(exc):
    return xml_error(exc.message, exc.status) if exc.xml else json_error(exc.message, exc.status)


@app.errorhandler(HTTPException)
def _handle_http_error(exc):
    """Unknown route, wrong method and similar: JSON with the right status for the API, Flask's page elsewhere."""
    if not (request.path.startswith("/api/") or request.path == "/run"):
        return exc
    response = exc.get_response()
    response.set_data(json.dumps({"status": "error", "message": exc.name}))
    response.content_type = "application/json"
    return response


@app.errorhandler(Exception)
def _handle_unexpected_error(exc):
    app.logger.error("Unhandled error on %s %s: %s", request.method, request.path, exc, exc_info=exc)
    return json_error("internal error (see the server log)", 500)


def guarded(what, status=500, xml=False):
    """Route decorator: an unexpected exception is logged in full and answered with a short message.
    `status` is 502 for routes whose work is a provider call (Yahoo, Databento), else 500."""
    def decorate(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (ApiError, HTTPException):
                raise
            except Exception as exc:
                app.logger.error("%s failed on %s: %s", what, request.path, exc, exc_info=exc)
                message = f"{what} failed (see the server log)"
                return xml_error(message, status) if xml else json_error(message, status)
        return inner
    return decorate


TICKER_RE = re.compile(r'^[A-Z0-9][A-Z0-9.^=\-]{0,14}$')


def ticker_arg(default='SPY', xml=False):
    ticker = (request.args.get('ticker') or default).strip().upper()
    if not TICKER_RE.match(ticker):
        raise ApiError("ticker is not a valid symbol", 400, xml)
    return ticker


def int_arg(name, default, minimum=1, xml=False, blank=None):
    """Integer query parameter. A missing value gives `default`; an empty one gives `blank` (default: `default`)."""
    raw = request.args.get(name)
    if raw is None:
        return default
    if raw.strip() == '':
        return default if blank is None else blank
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(f"{name} must be an integer", 400, xml)
    if minimum is not None and value < minimum:
        raise ApiError(f"{name} must be at least {minimum}", 400, xml)
    return value


def float_arg(name, default, minimum=None, xml=False):
    """Finite number query parameter. A missing or empty value gives `default`."""
    raw = request.args.get(name)
    if raw is None or raw.strip() == '':
        return default
    value = finite_float(raw, name, xml)
    if minimum is not None and value < minimum:
        raise ApiError(f"{name} must be at least {minimum}", 400, xml)
    return value


def finite_float(raw, name, xml=False):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ApiError(f"{name} must be a number", 400, xml)
    if not math.isfinite(value):
        raise ApiError(f"{name} must be a finite number", 400, xml)
    return value


def _num(value):
    """A finite float, or None for None, NaN, infinity and anything that is not a number."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _json_safe(obj):
    """Replace NaN and infinity with None (they are not valid JSON) and numpy scalars with Python numbers."""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, np.generic):
        obj = obj.item()
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def _live_spot(tk):
    """Latest close from Yahoo (the live price during the session), or None when it cannot be fetched."""
    try:
        closes = tk.history(period='5d')['Close'].dropna()
    except Exception as exc:
        app.logger.warning("spot price request failed: %s", exc)
        return None
    return _num(closes.iloc[-1]) if len(closes) else None


# ==========================================
# DAILY RUN FOLDERS
# ==========================================
def _run_folders():
    """Daily run folders of the data directory, newest first by the date in the name.

    Only folders named like `Sep-29-26` (`config.RUN_FOLDER_FORMAT`) count; the browser profile, the rejected
    downloads and every other subfolder are ignored. Returns None when the data directory does not exist."""
    try:
        names = os.listdir(DATA_DIR)
    except OSError:
        return None
    found = []
    for name in names:
        path = os.path.join(DATA_DIR, name)
        if not os.path.isdir(path):
            continue
        try:
            day = datetime.strptime(name, config.RUN_FOLDER_FORMAT)
        except ValueError:
            continue
        found.append((day, path))
    found.sort(reverse=True)
    return [path for _, path in found]


# ==========================================
# GAMMA EXPOSURE
# ==========================================
def build_gex_payload(spot, chains, expirations, today):
    """The /api/gex response data, computed by the pipeline's own `metrics.gex_profile()` so the live panel and the
    snapshot agree: net GEX by strike within 10% of spot, the walls, and the zero-gamma level (the spot at which
    aggregate net GEX changes sign), which is null with a reason when there is no sign change."""
    profile = metrics.gex_profile(spot, chains, expirations, today)
    if profile.get("status") != "fresh":
        raise ApiError(profile.get("reason") or "GEX profile unavailable.", 422)
    window = sorted((float(strike), value) for strike, value in (profile.get("by_strike_window") or {}).items())
    if not window:
        raise ApiError("No strikes with open interest within 10% of spot.", 422)
    return {
        "spot": spot,
        "zeroGamma": profile.get("zero_gamma"),
        "zeroGammaReason": profile.get("zero_gamma_reason"),
        "callWall": profile.get("call_wall"),
        "putWall": profile.get("put_wall"),
        "strikes": [strike for strike, _ in window],
        "gamma": [value for _, value in window],
    }


@app.route('/api/gex')
@guarded("GEX profile", 502)
def get_gex_profile():
    ticker_symbol = ticker_arg()
    tk = yf.Ticker(ticker_symbol)
    spot = _live_spot(tk)
    if spot is None:
        raise ApiError("Could not fetch the spot price.", 502)
    expirations = list(tk.options or [])
    if not expirations:
        raise ApiError("No options data available.", 404)

    chains = {}
    for exp in expirations[:metrics.GEX_PARAMS["expirations"]]:
        try:
            chain = tk.option_chain(exp)
        except Exception as exc:
            app.logger.warning("option chain %s %s failed: %s", ticker_symbol, exp, exc)
            continue
        chains[exp] = {"calls": chain.calls, "puts": chain.puts}
    if not chains:
        raise ApiError("Option chains could not be fetched.", 502)

    payload = build_gex_payload(spot, chains, list(chains), datetime.now().date())
    return jsonify({"status": "success", "data": payload})

def _rnd(value, digits=2):
    """_num rounded to `digits` places (None stays None, so the JSON never holds NaN)."""
    value = _num(value)
    return None if value is None else round(value, digits)


def build_arbitrage_series(ledger, limit):
    """Silver Eagle premium over silver futures from the macro ledger, paired within each run's row.
    Returns None when no row is a verified reading (both the futures price and the cheapest retail price above 0).
    The statistics are computed by the pipeline and read from the snapshot."""
    df = ledger.dropna(how='all').dropna(subset=['Datetime']).copy()
    df['ts'] = pd.to_datetime(df['Datetime'], format='mixed', errors='coerce')
    for col, name in (('COMEX_Silver', 'spot'), ('Retail_Silver_Cheapest', 'cheap'), ('Retail_Silver_Avg', 'avg')):
        df[name] = pd.to_numeric(df[col], errors='coerce') if col in df.columns else float('nan')
    df = df.dropna(subset=['ts']).sort_values('ts')
    points = [metrics.premium_point(r.ts, r.spot, r.cheap, r.avg) for r in df.itertuples()
              if metrics.verified_reading(r.spot, r.cheap)]
    if not points:
        return None
    points = points[-limit:]
    keys = ("spot", "cheapest_price", "avg_price", "cheapest_dollar", "avg_dollar", "cheapest_pct", "avg_pct")
    return {"labels": [p["label"] for p in points], **{k: [p[k] for p in points] for k in keys},
            "benchmark_symbol": "SI=F"}


def _snapshot_sections(conn):
    """(run_id, {"run_at", "ebay", "paper_physical", "shanghai"}) of the latest committed snapshot; (None, {}) without
    one. One snapshot parse per run, shared by the routes."""
    row = conn.execute("SELECT run_id FROM v2_latest_snapshot").fetchone()
    if not row:
        return None, {}
    key = f"snapshot_sections:{row['run_id']}"
    data = data_cache.get(key)
    if data is None:
        ctx = _v2lake.load_snapshot(conn, row["run_id"])
        data = {"run_at": _utc_to_local_label((ctx.get("run") or {}).get("generated_at")),
                **{n: ctx.get(n) or {} for n in ("ebay", "paper_physical", "shanghai")}}
        data_cache.set(key, data)
    return row["run_id"], data


def _eagle_book(ebay, run_at):
    """The dealer book as the payload shows it, from the snapshot's eBay section (None without listings)."""
    stored = ebay.get("listings") or []
    if not stored:
        return None
    flow = ebay.get("flow") or {}
    sold_prev, sold_7d = flow.get("sold_prev") or {}, flow.get("sold_7d") or {}
    listings = [{
        "item_id": l.get("item_id"), "title": l.get("title"), "seller": l.get("seller"), "url": l.get("url"),
        "total_cost": _rnd(l.get("total_cost")), "shipping": _rnd(l.get("shipping")),
        "premium_pct": _rnd(l.get("premium_percent")), "premium_dollars": _rnd(l.get("premium_dollars")),
        "status": l.get("status"), "available_qty": l.get("available_qty"), "sold_qty": l.get("sold_qty"),
        "sold_prev": sold_prev.get(l.get("item_id")), "sold_7d": sold_7d.get(l.get("item_id"))} for l in stored]
    listings.sort(key=lambda l: (l["total_cost"] is None, l["total_cost"] or 0))
    book = ebay.get("book") or {}    # a snapshot made before the book was stored has only the count
    return {"run_at": run_at, "count": book.get("count", len(listings)), "in_stock": book.get("in_stock"),
            "prev_count": flow.get("prev_count"), "cheapest_pct": book.get("cheapest_pct"),
            "median_pct": book.get("median_pct"), "listings": listings}


@app.route('/api/arbitrage_history', methods=['GET'])
@guarded("Arbitrage history")
def get_arbitrage_history():
    limit = int_arg('limit', 50)
    if not os.path.exists(LEDGER_CSV):
        return json_error("Ledger file not found.", 404)
    payload = build_arbitrage_series(data_cache.get_csv(LEDGER_CSV).copy(), limit)
    if payload is None:
        return json_error("Ledger is empty.", 404)

    # Statistics, dealer book and flow are the latest run's stored values; without them the series is still served.
    payload["stats"] = payload["book"] = payload["flow"] = None
    conn = _open_readonly()
    if conn is not None:
        try:
            _, snap = _snapshot_sections(conn)
            ebay = snap.get("ebay") or {}
            premium = ebay.get("premium") or {}
            payload["stats"] = premium.get("stats") if premium.get("status") == "fresh" else None
            flow = ebay.get("flow") or {}
            payload["flow"] = {k: v for k, v in flow.items() if k not in ("sold_prev", "sold_7d")} \
                if flow.get("status") == "fresh" else None
            payload["book"] = _eagle_book(ebay, snap.get("run_at"))
        except sqlite3.OperationalError as exc:
            if not _uninitialised(exc):
                raise
        finally:
            conn.close()
    return jsonify({"status": "success", "data": _json_safe(payload)})

# The Databento client is created on first use: only /api/darkpool needs it, so the terminal starts without a key.
db_client = None


def databento_client():
    global db_client
    if db_client is None:
        if not config.DATABENTO_API_KEY:
            raise ApiError("DATABENTO_API_KEY is not set in .env, so the dark pool panels are unavailable", 503)
        db_client = db.Historical(config.DATABENTO_API_KEY)
    return db_client

DARKPOOL_TRADE_LIMIT = 50000   # the request reads at most this many trades


def build_darkpool_payload(ticker, trades):
    """The /api/darkpool response data from a Databento trades frame, using the pipeline's `metrics.block_flow()`.

    Databento side `B` is a buy aggressor and `A` a sell aggressor (`N` unknown). Bias comes from aggressor volume
    when at least half of the block volume has a known side (`method` "aggressor"); otherwise the pipeline's
    VWAP heuristic classifies the unknown-side prints (`method` "vwap_heuristic"). Returns None when the window
    has trades but no block of at least `metrics.BLOCK_MIN_SIZE` shares."""
    flow = metrics.block_flow(trades, limit=DARKPOOL_TRADE_LIMIT)
    if not flow.get("blocks"):
        return None
    buy, sell = flow["buy_aggressor_volume"], flow["sell_aggressor_volume"]
    if flow["bias_method"] == "vwap_heuristic":
        heuristic = flow["vwap_heuristic_unknown_side"]
        buy, sell = buy + heuristic["at_or_above_vwap"], sell + heuristic["below_vwap"]
    side_label = {"buy": "BUY", "sell": "SELL"}
    return {
        "ticker": ticker,
        "total_block_volume": int(flow["block_volume"]),
        "total_notional_usd": float(flow["block_notional"]),
        "largest_single_block": int(flow["largest_block"]),
        "vwap_price": float(flow["block_vwap"]),
        "sentiment": {
            "bias": flow["bias"],
            "bull_volume": int(buy),
            "bear_volume": int(sell),
            "method": flow["bias_method"],
        },
        "note": flow.get("reason"),
        "recent_prints": [{
            "time": pd.Timestamp(p["time"]).strftime("%H:%M:%S"),
            "price": float(p["price"]),
            "size": int(p["size"]),
            "side": side_label.get(p["aggressor"], "UNKNOWN"),
        } for p in flow["recent_prints"]],
    }


@app.route('/api/darkpool')
@guarded("Dark pool request", 502)
def get_dark_pool_profile():
    ticker = ticker_arg()

    # --- THE T+1 HISTORICAL BARRIER FIX ---
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # Databento's Historical API batches the tape overnight.
    # We must anchor our 'end' to Midnight UTC of YESTERDAY to guarantee the file exists.
    yesterday = now - timedelta(days=1)
    available_end = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)

    # Find the most recently completed trading session
    # weekday(): 0=Mon, 1=Tue, ..., 5=Sat, 6=Sun
    if available_end.weekday() == 6: # Sunday Midnight UTC -> Shift to Saturday Midnight
        end_time = available_end - timedelta(days=1)
    elif available_end.weekday() == 0: # Monday Midnight UTC -> Shift to Saturday Midnight
        end_time = available_end - timedelta(days=2)
    else:
        end_time = available_end

    # Look back exactly 24 hours from our safe 'end_time' to capture the full session
    start_time = end_time - timedelta(days=1)

    # 1. THE QUERY: Fetch tick-level trades from the Consolidated Tape
    data = databento_client().timeseries.get_range(
        dataset='DBEQ.BASIC',
        schema='trades',
        symbols=[ticker],
        start=start_time.isoformat(),
        end=end_time.isoformat(),
        limit=DARKPOOL_TRADE_LIMIT
    )

    # Convert the raw binary stream into a Pandas DataFrame
    df = data.to_df()

    if df.empty:
        raise ApiError("No trades found in the target window.", 404)

    # 2. THE FILTER, THE MATH AND THE SENTIMENT: the pipeline's block-flow definition
    payload = build_darkpool_payload(ticker, df)
    if payload is None:
        return jsonify({"status": "success", "message": "No institutional blocks detected.", "data": None})
    return jsonify({"status": "success", "data": payload})

# --- ENDPOINT: VMRI DATA PROXY ---
@app.route('/api/vmri_history')
def get_vmri_history():
    """Returns the time-series history of VMRI scores, math derivatives, and macro context."""
    try:
        if not os.path.exists(LEDGER_CSV):
            return jsonify({"status": "error", "message": "Ledger not found", "error": "Ledger not found"}), 404
            
        df = pd.read_csv(LEDGER_CSV)
        
        # 1. THE FIX: Drop completely empty rows or rows missing a Datetime BEFORE parsing
        df = df.dropna(how='all')
        df = df.dropna(subset=['Datetime'])
        
        df['Datetime'] = pd.to_datetime(df['Datetime'], format='mixed')
        
        # Drop any dates that failed to parse (NaT)
        df = df.dropna(subset=['Datetime'])
        
        df = df.sort_values('Datetime').tail(500) # Last 500 records

        # ---------------------------------------------------------
        # THE MATH ENGINE (Phase 1 Upgrades)
        # ---------------------------------------------------------
        
        # 1. Moving Average (10-Period SMA)
        # Acts as a mechanical trendline/crossover trigger
        df['VMRI_SMA_10'] = df['VMRI_Score'].rolling(window=10).mean()

        # 2. Momentum / Rate of Change (5-Period Delta)
        # Measures velocity. Positive = accelerating risk, Negative = decaying risk
        df['VMRI_Momentum_5'] = df['VMRI_Score'].diff(periods=5)

        # MMRI (Mannarino), the base_stress of every VMRI row, from the ledger's stored DXY and 10Y_Yield. The formula
        # lives in core/metrics.py; the CSV ledger keeps its old header, so it has no MMRI column to read.
        dxy, tny = pd.to_numeric(df['DXY'], errors='coerce'), pd.to_numeric(df['10Y_Yield'], errors='coerce')
        df['MMRI'] = metrics.mmri_value(dxy, tny)
        df['MMRI_SMA_10'] = df['MMRI'].rolling(window=10).mean()
        df['MMRI_Momentum_5'] = df['MMRI'].diff(periods=5)

        # 3. The "Primary Driver" Logic
        # Look at the absolute % change of the 4 core pillars over the last 5 periods.
        drivers_df = pd.DataFrame({
            'DXY': dxy.pct_change(periods=5).abs(),
            '10Y Yield': tny.pct_change(periods=5).abs(),
            'VIX': pd.to_numeric(df['VIX'], errors='coerce').pct_change(periods=5).abs(),
            'High Yield OAS': pd.to_numeric(df['High_Yield_OAS'], errors='coerce').pct_change(periods=5).abs()
        })
        
        # Safely find the max, ignoring rows that are entirely NaN (like the first 5 rows)
        def get_driver(row):
            if row.isna().all():
                return "AWAITING DATA"
            return row.idxmax()
            
        df['Primary_Driver'] = drivers_df.apply(get_driver, axis=1)
        
        # ---------------------------------------------------------

        labels = df['Datetime'].dt.strftime('%Y-%m-%d %H:%M').tolist()
        
        # THE ULTIMATE LIST CLEANER
        def clean_list(lst):
            cleaned = []
            for val in lst:
                if pd.isna(val) or val in ["NaN", "nan", "None", ""]:
                    cleaned.append(None)
                else:
                    try:
                        f = float(val)
                        if math.isnan(f) or math.isinf(f):
                            cleaned.append(None)
                        else:
                            cleaned.append(f)
                    except (ValueError, TypeError):
                        cleaned.append(None)
            return cleaned
        
        # Ensure string columns (like drivers) safely handle NaNs before tolist()
        driver_list = df['Primary_Driver'].astype(str).tolist()

        data = {
            "labels": labels,
            "scores": clean_list(df['VMRI_Score'].tolist()),
            
            # New Math Data
            "sma_10": clean_list(df['VMRI_SMA_10'].tolist()),
            "momentum_5": clean_list(df['VMRI_Momentum_5'].tolist()),
            "mmri": clean_list(df['MMRI'].tolist()),
            "mmri_sma_10": clean_list(df['MMRI_SMA_10'].tolist()),
            "mmri_momentum_5": clean_list(df['MMRI_Momentum_5'].tolist()),
            "primary_driver": driver_list,
            
            "context": {
                "dxy": clean_list(df['DXY'].tolist()),
                "yield": clean_list(df['10Y_Yield'].tolist()),
                "vix": clean_list(df['VIX'].tolist()),
                "gold": clean_list(df['Gold_Price'].tolist()),
                "gsr": clean_list(df['Gold_Silver_Ratio'].tolist()),
                "oas": clean_list(df['High_Yield_OAS'].tolist())
            }
        }
        return jsonify(data)
    except Exception as e:
        app.logger.error("VMRI history failed: %s", e, exc_info=e)
        return jsonify({"status": "error", "message": "VMRI history failed (see the server log)",
                        "error": "VMRI history failed (see the server log)"}), 500

# --- ENDPOINT: VMRI CHART VIEWER ---
# Per-index settings for the shared chart page. "tiers" run from the lowest band up; the first has no lower limit.
CHART_INDEXES = {
    "vmri": {"name": "VMRI", "title": "VMRI Macro Risk Index", "scores": "scores", "sma": "sma_10", "mom": "momentum_5",
             "credit": "", "formula": "(DXY*TNX/1.61) * (OAS/4.00) * (VIX/20.00)", "showDriver": True,
             "overlays": ["DXY", "VIX", "10Y", "OAS"], "tiers": [
                 [None, "LOW RISK", "LOW", "#22c55e", "34,197,94"], [150, "MODERATE RISK", "MODERATE", "#eab308", "234,179,8"],
                 [250, "ELEVATED RISK", "ELEVATED", "#f97316", "249,115,22"], [350, "SYSTEMIC THREAT", "SYSTEMIC", "#ef4444", "239,68,68"]]},
    "mmri": {"name": "MMRI", "title": "Mannarino Market Risk Indicator", "scores": "mmri", "sma": "mmri_sma_10", "mom": "mmri_momentum_5",
             "overlays": ["DXY", "10Y"], "credit": "MMRI by Gregory Mannarino \u00b7 (DXY \u00d7 10Y) \u00f7 1.61 \u00b7 VMRI extends it with credit spreads and VIX",
             "formula": "(DXY*TNX)/1.61", "showDriver": False,
             "tiers": [[lo, name, name.split()[0], color, rgb] for (lo, name), (color, rgb) in zip(metrics.MMRI_TIERS, (
                 ("#22c55e", "34,197,94"), ("#eab308", "234,179,8"), ("#f97316", "249,115,22"), ("#ef4444", "239,68,68")))]},
}

@app.route('/vmri_chart')
@app.route('/mmri_chart', defaults={'index': 'mmri'})
def vmri_chart_page(index='vmri'):
    """The index chart page shown in a terminal frame: VMRI in panel 1, MMRI in panel 17 (/mmri_chart). Both draw /api/vmri_history."""
    cfg = CHART_INDEXES[index]
    html_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>__TITLE__</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js" integrity="sha384-dug+JxfBvklEQdJ4AYuBBAIScUz0bVN73xpy273gcAwHjb3qI0fXmuYNaNfdyYJG" crossorigin="anonymous"></script>
        <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@2.1.0/dist/chartjs-plugin-annotation.min.js" integrity="sha384-dB7WWqy9+vERwKo2atAKw+KLNhfY0RX5ZjV1l4HHMBZz5wfSvX/GsytIpOyBY8dG" crossorigin="anonymous"></script>
        <style>
            * { box-sizing: border-box; }
            body { font-family: 'JetBrains Mono', ui-monospace, Menlo, monospace; background: #000; color: #a1a1aa; margin: 0; padding: 10px 12px; height: 100vh; display: flex; flex-direction: column; overflow: hidden; }
            .head { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 6px 14px; margin-bottom: 6px; }
            .cap { font-size: 8px; text-transform: uppercase; letter-spacing: 1.5px; color: #71717a; }
            .big { display: flex; align-items: center; gap: 8px; }
            .score { font-size: 28px; font-weight: 800; line-height: 1.1; color: #fff; font-variant-numeric: tabular-nums; }
            .chip { font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; padding: 1px 5px; border: 1px solid #3f3f46; border-radius: 3px; color: #a1a1aa; }
            .mid { font-size: 9px; line-height: 1.6; padding-top: 10px; }
            .ctl { margin-left: auto; display: flex; flex-wrap: wrap; gap: 6px; }
            .seg { display: flex; }
            .seg button { font: inherit; font-size: 9px; background: #09090b; border: 1px solid #27272a; color: #a1a1aa; padding: 2px 7px; cursor: pointer; margin-left: -1px; }
            .seg button:first-child { margin-left: 0; border-radius: 3px 0 0 3px; }
            .seg button:last-child { border-radius: 0 3px 3px 0; }
            .seg button[aria-pressed="true"] { color: #fff; border-color: #3f3f46; background: #18181b; position: relative; }
            .chart-wrap { flex: 1; min-height: 0; position: relative; }
            .foot { font-size: 9px; color: #71717a; margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        </style>
    </head>
    <body>
        <div class="head">
            <div>
                <div class="cap" id="cap">__NAME__</div>
                <div class="big"><span class="score" id="score">&mdash;</span><span class="chip" id="chip" hidden></span></div>
            </div>
            <div class="mid" id="mid"></div>
            <div class="ctl">
                <div class="seg" id="ranges"></div>
                <div class="seg" id="overlays"></div>
            </div>
        </div>
        <div class="chart-wrap"><canvas id="vmriChart"></canvas></div>
        <div class="foot" id="foot"></div>
        <div class="foot" id="credit" style="white-space: normal"></div>

        <script>
            var CFG = __CFG__;
            var NAME = CFG.name;
            var TIERS = CFG.tiers.map(function (t) {
                return { min: t[0] === null ? -Infinity : t[0], name: t[1], band: t[2], color: t[3], rgb: t[4] };
            }).reverse();
            document.getElementById('credit').textContent = CFG.credit;
            var RANGES = { '1M': 30, '3M': 91, 'ALL': null };
            var OVERLAYS = { 'DXY': 'dxy', 'VIX': 'vix', '10Y': 'yield', 'OAS': 'oas' };
            var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
            var DAY = 86400000;

            Chart.defaults.font.family = "'JetBrains Mono', ui-monospace, Menlo, monospace";

            var rows = [];
            var range = '3M';
            var overlay = null;
            var chart = null;

            function $(id) { return document.getElementById(id); }
            function tierOf(v) { return TIERS.find(function (t) { return v >= t.min; }); }
            function isNum(v) { return typeof v === 'number' && isFinite(v); }
            function pad(n) { return (n < 10 ? '0' : '') + n; }
            function dayText(t) { var d = new Date(t); return MONTHS[d.getMonth()] + ' ' + d.getDate(); }
            function timeText(t) { var d = new Date(t); return pad(d.getHours()) + ':' + pad(d.getMinutes()); }

            // Selected range: rows from the cutoff to the newest label, or everything when fewer than 2 scores fall inside.
            function pickView() {
                var days = RANGES[range], last = rows[rows.length - 1].t;
                var min = days === null ? rows[0].t : last - days * DAY;
                var view = rows.filter(function (r) { return r.t >= min; });
                if (view.filter(function (r) { return isNum(r.y); }).length < 2) { view = rows; min = rows[0].t; }
                return { view: view, min: min, max: last };
            }

            // Y bounds that fit the visible scores, in steps of 50.
            function yBounds(view) {
                var ys = view.map(function (r) { return r.y; }).filter(isNum);
                return {
                    min: Math.max(0, Math.floor((Math.min.apply(null, ys) - 10) / 50) * 50),
                    max: Math.ceil((Math.max.apply(null, ys) + 10) / 50) * 50
                };
            }

            function lastIndex(list, key) {
                for (var i = list.length - 1; i >= 0; i--) { if (isNum(list[i][key])) return i; }
                return -1;
            }

            function setText(id, text, color) {
                var el = $(id);
                el.textContent = text;
                el.style.color = color || '';
            }

            function line(text, color) {
                var d = document.createElement('div');
                d.textContent = text;
                if (color) d.style.color = color;
                $('mid').appendChild(d);
            }

            function renderHeader(d, drivers) {
                var n = rows.length - 1, r = rows[n];
                $('cap').textContent = NAME + ' · ' + dayText(r.t) + ' ' + timeText(r.t);
                $('mid').textContent = '';
                var chip = $('chip');
                chip.hidden = false;
                if (!isNum(r.y)) {
                    var k = lastIndex(rows, 'y');
                    setText('score', '—');
                    chip.textContent = 'NO SCORE';
                    chip.style.cssText = '';
                    line('Latest run (' + dayText(r.t) + ' ' + timeText(r.t) + ') recorded no score: an input was missing.');
                    line('Last recorded: ' + rows[k].y.toFixed(1) + ' on ' + dayText(rows[k].t) + ' ' + timeText(rows[k].t));
                    return;
                }
                var tier = tierOf(r.y);
                setText('score', r.y.toFixed(1));
                chip.textContent = tier.name;
                chip.style.cssText = 'color:' + tier.color + ';border-color:rgba(' + tier.rgb + ',0.4);background:rgba(' + tier.rgb + ',0.1)';
                var m = d[CFG.mom][n];
                if (isNum(m)) line((m > 0 ? '▲ +' : m < 0 ? '▼ ' : '') + m.toFixed(1) + ' over 5 runs', m > 0 ? '#f87171' : m < 0 ? '#4ade80' : '');
                var drv = drivers[n];
                if (CFG.showDriver && drv && drv !== 'AWAITING DATA') line('Driver: ' + drv);
            }

            function renderFooter(view) {
                var ys = view.map(function (r) { return r.y; }).filter(isNum);
                var avg = lastIndex(rows, 'avg');
                var text = ys.length + ' readings in view · low ' + Math.min.apply(null, ys).toFixed(1) +
                    ' · high ' + Math.max.apply(null, ys).toFixed(1) +
                    (avg >= 0 ? ' · 10-run avg ' + rows[avg].avg.toFixed(1) : '');
                if (overlay) text += ' · right axis: ' + overlay;
                setText('foot', text);
            }

            function showMessage(text, color) {
                if (chart) { chart.destroy(); chart = null; }
                setText('foot', text, color);
            }

            function annotations(yb) {
                var a = {}, lo = TIERS.slice().reverse().map(function (t) { return isFinite(t.min) ? t.min : 0; }), hi = lo.slice(1).concat(Infinity);
                TIERS.slice().reverse().forEach(function (t, i) {
                    var y0 = Math.max(lo[i], yb.min), y1 = Math.min(hi[i], yb.max);
                    if (y1 > y0) a['box' + i] = {
                        type: 'box', yMin: y0, yMax: y1, backgroundColor: 'rgba(' + t.rgb + ',0.07)', borderWidth: 0,
                        label: { display: true, content: t.band, position: { x: 'start', y: 'start' }, color: 'rgba(' + t.rgb + ',0.55)', font: { size: 8, weight: 'normal' }, padding: 3 }
                    };
                    if (i > 0 && lo[i] > yb.min && lo[i] < yb.max) a['line' + i] = {
                        type: 'line', yMin: lo[i], yMax: lo[i], borderColor: 'rgba(' + t.rgb + ',0.45)', borderWidth: 1, borderDash: [4, 4],
                        label: { display: true, content: String(lo[i]), position: 'end', yAdjust: -7, color: t.color, backgroundColor: 'rgba(0,0,0,0)', font: { size: 8, weight: 'normal' }, padding: 1 }
                    };
                });
                return a;
            }

            var crosshair = {
                id: 'crosshair',
                beforeDatasetsDraw: function (c) {
                    var act = c.tooltip && c.tooltip.getActiveElements();
                    if (!act || !act.length) return;
                    var x = act[0].element.x, ctx = c.ctx;
                    ctx.save();
                    ctx.strokeStyle = '#3f3f46';
                    ctx.lineWidth = 1;
                    ctx.beginPath();
                    ctx.moveTo(x, c.chartArea.top);
                    ctx.lineTo(x, c.chartArea.bottom);
                    ctx.stroke();
                    ctx.restore();
                }
            };

            function renderChart() {
                var p = pickView(), view = p.view, yb = yBounds(view);
                var lastIdx = lastIndex(view, 'y');
                var datasets = [
                    {
                        label: NAME, data: view.map(function (r) { return { x: r.t, y: r.y }; }), parsing: false,
                        borderColor: '#a1a1aa', borderWidth: 1.75, tension: 0.15, spanGaps: true, yAxisID: 'y',
                        pointRadius: function (c) { return c.dataIndex === lastIdx ? 4 : 0; }, pointHoverRadius: 3,
                        pointBackgroundColor: function (c) { return c.raw && isNum(c.raw.y) ? tierOf(c.raw.y).color : '#a1a1aa'; },
                        pointBorderWidth: 0,
                        // a run that recorded no score is bridged with a faint dashed line, so it reads as a gap, not as a value
                        segment: {
                            borderColor: function (c) { return c.p0.skip || c.p1.skip ? 'rgba(161,161,170,0.4)' : tierOf(c.p1.parsed.y).color; },
                            borderDash: function (c) { return c.p0.skip || c.p1.skip ? [2, 3] : undefined; }
                        }
                    },
                    {
                        label: '10-run avg', data: view.map(function (r) { return { x: r.t, y: r.avg }; }), parsing: false,
                        borderColor: 'rgba(255,255,255,0.35)', borderWidth: 1, borderDash: [4, 4], pointRadius: 0, pointHoverRadius: 0, yAxisID: 'y'
                    }
                ];
                if (overlay) datasets.push({
                    label: overlay, data: view.map(function (r) { return { x: r.t, y: r[OVERLAYS[overlay]] }; }), parsing: false,
                    borderColor: '#38bdf8', borderWidth: 1.25, borderDash: [2, 2], pointRadius: 0, pointHoverRadius: 0, yAxisID: 'y1'
                });
                if (chart) chart.destroy();
                chart = new Chart($('vmriChart').getContext('2d'), {
                    type: 'line',
                    data: { datasets: datasets },
                    plugins: [crosshair],
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        animation: false,
                        interaction: { mode: 'nearest', axis: 'x', intersect: false },
                        plugins: {
                            legend: { display: false },
                            annotation: { annotations: annotations(yb) },
                            tooltip: {
                                backgroundColor: 'rgba(9,9,11,0.95)', borderColor: '#27272a', borderWidth: 1, padding: 8, displayColors: false,
                                titleColor: '#a1a1aa', bodyColor: '#e4e4e7',
                                titleFont: { size: 10 }, bodyFont: { size: 11 },
                                filter: function (item, i) { return i === 0; },
                                callbacks: {
                                    title: function (items) { return dayText(items[0].parsed.x) + ', ' + timeText(items[0].parsed.x); },
                                    label: function (item) {
                                        // The rows come from the run at that x, whichever line was hit.
                                        var r = view.find(function (v) { return v.t === item.parsed.x; });
                                        if (!r) return '';
                                        var out = [isNum(r.y) ? NAME + ' ' + r.y.toFixed(1) + ' · ' + tierOf(r.y).name : NAME + ' no score'];
                                        if (isNum(r.avg)) out.push('10-run avg ' + r.avg.toFixed(1));
                                        if (overlay && isNum(r[OVERLAYS[overlay]])) out.push(overlay + ' ' + r[OVERLAYS[overlay]].toFixed(2));
                                        return out;
                                    }
                                }
                            }
                        },
                        scales: {
                            x: {
                                type: 'linear', min: p.min, max: p.max, grid: { display: false }, border: { color: '#27272a' },
                                ticks: { color: '#71717a', font: { size: 9 }, maxRotation: 0, align: 'inner', callback: function (v) { return dayText(v); } },
                                afterBuildTicks: function (axis) {
                                    var step = Math.max(1, Math.ceil((axis.max - axis.min) / DAY / 5)) * DAY, ticks = [];
                                    for (var v = new Date(axis.max).setHours(0, 0, 0, 0); v >= axis.min; v -= step) ticks.unshift({ value: v });
                                    axis.ticks = ticks;
                                }
                            },
                            y: { min: yb.min, max: yb.max, grid: { color: '#18181b' }, border: { display: false }, ticks: { color: '#71717a', font: { size: 9 } } },
                            y1: { display: !!overlay, position: 'right', grid: { display: false }, ticks: { color: '#38bdf8', font: { size: 9 } } }
                        }
                    }
                });
                renderFooter(view);
            }

            function makeButtons(id, names, isOn, onClick) {
                var box = $(id);
                box.textContent = '';
                names.forEach(function (name) {
                    var b = document.createElement('button');
                    b.type = 'button';
                    b.textContent = name;
                    b.setAttribute('aria-pressed', String(isOn(name)));
                    b.addEventListener('click', function () { onClick(name); });
                    box.appendChild(b);
                });
            }

            function refresh() {
                makeButtons('ranges', Object.keys(RANGES), function (n) { return n === range; }, function (n) { range = n; refresh(); });
                makeButtons('overlays', CFG.overlays, function (n) { return n === overlay; }, function (n) { overlay = overlay === n ? null : n; refresh(); });
                renderChart();
            }

            async function loadChart() {
                try {
                    var response = await fetch('/api/vmri_history');
                    var d = await response.json();
                    if (!response.ok || d.status === 'error' || !d.labels) {
                        showMessage('Could not load the ' + NAME + ' history: ' + ((d && d.message) || 'the server returned an error.'), '#f87171');
                        return;
                    }
                    var ctxs = d.context || {};
                    rows = d.labels.map(function (l, i) {
                        var r = { t: new Date(l.replace(' ', 'T')).getTime(), y: d[CFG.scores][i], avg: d[CFG.sma][i] };
                        Object.keys(OVERLAYS).forEach(function (k) { var key = OVERLAYS[k]; r[key] = (ctxs[key] || [])[i]; });
                        return r;
                    });
                    if (!rows.length || lastIndex(rows, 'y') < 0) {
                        showMessage('No ' + NAME + ' history recorded yet.');
                        return;
                    }
                    renderHeader(d, Array.isArray(d.primary_driver) ? d.primary_driver : []);
                    refresh();
                } catch (e) {
                    console.error(NAME + ' chart failed:', e);
                    showMessage('Could not load the ' + NAME + ' history.', '#f87171');
                }
            }

            loadChart();
        </script>
    </body>
    </html>
    """
    # No Jinja: the config is JSON spliced into a static page, so braces in it can never be read as template syntax.
    return html_template.replace("__TITLE__", cfg["title"]).replace("__NAME__", cfg["name"]).replace("__CFG__", json.dumps(cfg))

@app.route('/api/war_room', methods=['GET', 'POST'])
@guarded("War room")
def api_war_room():
    """
    The SecDB Lite Impact Engine.
    Accepts shift vectors for DXY, 10Y Yield, OAS, and VIX.
    Recalculates the VMRI and returns the hypothetical environment.
    The starting values come from the macro ledger; when it lacks one the answer is a 503 naming it
    (no stand-in constants).
    """
    # Handle both GET (URL params) and POST (JSON body)
    if request.method == 'POST':
        data = request.get_json(silent=True) if request.get_data() else {}
        if not isinstance(data, dict):
            raise ApiError("request body must be a JSON object", 400)
    else:
        data = request.args

    # 1. Parse Shift Vectors
    dxy_shift = finite_float(data.get('dxy_shift', 0.0), 'dxy_shift')
    tnx_shift = finite_float(data.get('tnx_shift', 0.0), 'tnx_shift')
    oas_shift = finite_float(data.get('oas_shift', 0.0), 'oas_shift')
    vix_shift = finite_float(data.get('vix_shift', 0.0), 'vix_shift')
    vix_shift_pct = finite_float(data.get('vix_shift_pct', 0.0), 'vix_shift_pct')

    # 2. Ingest the Latest Valid Live Environment
    if not os.path.exists(LEDGER_CSV):
        raise ApiError("macro ledger not found: run the pipeline", 503)
    df = pd.read_csv(LEDGER_CSV)
    # CRITICAL FIX: Drop completely empty rows that might be at the end of the CSV
    df = df.dropna(how='all')
    if df.empty:
        raise ApiError("macro ledger is empty: run the pipeline", 503)
    latest = df.iloc[-1]

    # Reads a ledger column as a float. If the absolute last row has no value for it, crawl backwards up the
    # CSV to the last known good value. A column with no value at all is an error naming it.
    def ledger_value(col_name):
        f = _num(latest.get(col_name))
        if f is not None:
            return f
        if col_name in df:
            last_valid = pd.to_numeric(df[col_name], errors='coerce').dropna()
            if len(last_valid):
                return float(last_valid.iloc[-1])
        raise ApiError(f"the macro ledger has no {col_name} value", 503)

    # Pull the real data (or crawl back to find it)
    base_dxy = ledger_value('DXY')
    base_tnx = ledger_value('10Y_Yield')
    base_oas = ledger_value('High_Yield_OAS')
    base_vix = ledger_value('VIX')
    base_vmri = ledger_value('VMRI_Score')

    # 3. Apply the Shift Vectors to create the Hypothetical Environment
    hypo_dxy = base_dxy + dxy_shift
    hypo_tnx = base_tnx + tnx_shift
    hypo_oas = base_oas + oas_shift
    
    # Calculate VIX shift 
    if vix_shift_pct != 0.0:
        hypo_vix = base_vix * (1 + (vix_shift_pct / 100.0))
    else:
        hypo_vix = base_vix + vix_shift

    # 4. The Math Engine: Recalculate VMRI
    def parts(dxy, tnx, oas, vix):
        base, credit, vol = (dxy * tnx) / 1.61, oas / 4.00, vix / 20.00
        return base, credit, vol, base * credit * vol

    def tier_of(v):
        if v < 150:
            return "LOW RISK (Complacent / Squeeze Danger)"
        if v < 250:
            return "MODERATE RISK (Standard Operating Environment)"
        if v < 350:
            return "ELEVATED RISK (Hedge Triggers Active)"
        return "SYSTEMIC THREAT (Crash Dynamics Active)"

    base_stress, credit_multiplier, vol_premium, hypo_vmri = parts(hypo_dxy, hypo_tnx, hypo_oas, hypo_vix)
    live_stress, live_credit, live_vol, live_formula_vmri = parts(base_dxy, base_tnx, base_oas, base_vix)

    # 5. Determine the New Threat Tier
    tier = tier_of(hypo_vmri)

    # 6. Calculate the Impact Delta
    vmri_delta = hypo_vmri - base_vmri
    vmri_delta_pct = (vmri_delta / base_vmri) * 100 if base_vmri != 0 else None

    # What each lever does on its own (the others left at live values). The four effects do not add up to the
    # total because the variables are multiplied.
    solo = {
        "dxy": parts(hypo_dxy, base_tnx, base_oas, base_vix)[3] - live_formula_vmri,
        "tnx": parts(base_dxy, hypo_tnx, base_oas, base_vix)[3] - live_formula_vmri,
        "oas": parts(base_dxy, base_tnx, hypo_oas, base_vix)[3] - live_formula_vmri,
        "vix": parts(base_dxy, base_tnx, base_oas, hypo_vix)[3] - live_formula_vmri,
    }

    # Where the live and scenario scores sit among the scores this installation has recorded
    history = None
    scores = pd.to_numeric(df.get('VMRI_Score'), errors='coerce').dropna() if 'VMRI_Score' in df else pd.Series(dtype=float)
    if len(scores) >= 20:
        when = pd.to_datetime(df.loc[scores.index, 'Datetime'], errors='coerce', format='mixed').dropna()
        lo, hi = math.floor(scores.min() / 10) * 10, math.ceil(max(scores.max(), 360) / 10) * 10
        counts, _ = np.histogram(scores, bins=30, range=(lo, hi))
        history = {
            "n": int(len(scores)), "start": when.min().date().isoformat() if len(when) else None,
            "end": when.max().date().isoformat() if len(when) else None,
            "min": round(float(scores.min()), 1), "median": round(float(scores.median()), 1), "max": round(float(scores.max()), 1),
            "pct_below_current": round(float((scores < base_vmri).mean() * 100), 1),
            "pct_below_hypothetical": round(float((scores < hypo_vmri).mean() * 100), 1),
            "lo": lo, "hi": hi, "counts": [int(c) for c in counts],
        }

    # 7. Construct the Output Payload
    payload = {
        "status": "success",
        "formula": "VMRI = (DXY x 10Y yield / 1.61) x (HY OAS / 4) x (VIX / 20)",
        "thresholds": {"moderate": 150, "elevated": 250, "systemic": 350},
        "current": {
            "vmri": round(base_vmri, 2),
            "tier": tier_of(base_vmri),
            "dxy": round(base_dxy, 2),
            "tnx": round(base_tnx, 2),
            "oas": round(base_oas, 2),
            "vix": round(base_vix, 2),
            "factors": {"macro_base": round(live_stress, 2), "credit": round(live_credit, 3), "vol": round(live_vol, 3)}
        },
        "hypothetical": {
            "vmri": round(hypo_vmri, 2),
            "tier": tier,
            "dxy": round(hypo_dxy, 2),
            "tnx": round(hypo_tnx, 2),
            "oas": round(hypo_oas, 2),
            "vix": round(hypo_vix, 2),
            "factors": {"macro_base": round(base_stress, 2), "credit": round(credit_multiplier, 3), "vol": round(vol_premium, 3)}
        },
        "impact": {
            "vmri_delta": round(vmri_delta, 2),
            "vmri_delta_pct": round(vmri_delta_pct, 2) if vmri_delta_pct is not None else None,
            "solo_delta": {k: round(v, 2) for k, v in solo.items()}
        },
        "history": history
    }

    return jsonify(payload)


@app.route('/api/silver_eagle_prices', methods=['POST'])
def api_silver_eagle_prices():
    """Runs ebay.py: eBay listings for the tracked Silver Eagles. It appends a row to the arbitrage ledger, so it is
    POST only and covered by the cross-site guard."""
    try:
        # Run the external ebay.py script. Playwright is not used any more, but the eBay calls take a while.
        proc = subprocess.run([PYTHON_BIN, EBAY_SCRIPT_PATH], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return xml_error("Scan timed out. eBay might be blocking connections.", 504)
    except Exception as e:
        app.logger.error("Silver Eagle scan could not start: %s", e, exc_info=e)
        return xml_error("Silver Eagle scan could not start (see the server log).", 500)
    if proc.returncode != 0:
        app.logger.error("ebay.py exited with code %s: %s", proc.returncode,
                         _v2lake.redact((proc.stdout or "") + (proc.stderr or ""))[-2000:])
        return xml_error("Silver Eagle scan failed (see the server log).", 500)

    # The script prints pure XML to stdout, so we just return it
    return Response(proc.stdout, mimetype='application/xml')

# --- ENDPOINT 1: THE DATA PROXY ---
@app.route('/api/inventory_data')
def get_inventory_data():
    """Reads the CSV and returns JSON for the JS Chart."""
    try:
        if not os.path.exists(INVENTORY_CSV):
            return jsonify({"status": "error", "message": "CSV not found", "error": "CSV not found"}), 404
            
        # Read CSV and ensure dates are sorted
        df = pd.read_csv(INVENTORY_CSV)
        df['Date'] = pd.to_datetime(df['Date'])
        df = df.sort_values('Date')
        
        # Convert to dictionary format for JSON
        data = {
            "labels": df['Date'].dt.strftime('%Y-%m-%d').tolist(),
            "registered": df['Registered'].tolist(),
            "eligible": df['Eligible'].tolist(),
            "total": df['Total'].tolist()
        }
        return jsonify(data)
    except Exception as e:
        app.logger.error("Inventory data failed: %s", e, exc_info=e)
        return jsonify({"status": "error", "message": "Inventory data failed (see the server log)",
                        "error": "Inventory data failed (see the server log)"}), 500



# --- ENDPOINT 2: THE DASHBOARD VIEWER ---
@app.route('/inventory_chart')
def inventory_chart_page():
    """The COMEX inventory chart page shown in the terminal's panel 2 frame. It draws /api/inventory_data."""
    html_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>COMEX Silver Inventory</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js" integrity="sha384-dug+JxfBvklEQdJ4AYuBBAIScUz0bVN73xpy273gcAwHjb3qI0fXmuYNaNfdyYJG" crossorigin="anonymous"></script>
        <style>
            * { box-sizing: border-box; }
            body { font-family: 'JetBrains Mono', ui-monospace, Menlo, monospace; background: #000; color: #a1a1aa; margin: 0; padding: 10px 12px; height: 100vh; display: flex; flex-direction: column; overflow: hidden; }
            .head { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 6px 14px; margin-bottom: 6px; }
            .cap { font-size: 8px; text-transform: uppercase; letter-spacing: 1.5px; color: #71717a; }
            .big { display: flex; align-items: center; gap: 8px; }
            .score { font-size: 28px; font-weight: 800; line-height: 1.1; color: #fff; font-variant-numeric: tabular-nums; }
            .unit { font-size: 11px; font-weight: 400; color: #71717a; }
            .mid { font-size: 9px; line-height: 1.6; padding-top: 10px; }
            .mid span { white-space: nowrap; }
            .ctl { margin-left: auto; display: flex; flex-wrap: wrap; gap: 6px; }
            .seg { display: flex; }
            .seg button { font: inherit; font-size: 9px; background: #09090b; border: 1px solid #27272a; color: #a1a1aa; padding: 2px 7px; cursor: pointer; margin-left: -1px; }
            .seg button:first-child { margin-left: 0; border-radius: 3px 0 0 3px; }
            .seg button:last-child { border-radius: 0 3px 3px 0; }
            .seg button[aria-pressed="true"] { color: #fff; border-color: #3f3f46; background: #18181b; position: relative; }
            .chart-wrap { flex: 1; min-height: 0; position: relative; }
            .foot { font-size: 9px; color: #71717a; margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        </style>
    </head>
    <body>
        <div class="head">
            <div>
                <div class="cap" id="cap">REGISTERED</div>
                <div class="big"><span class="score"><span id="score">&mdash;</span><span class="unit" id="unit"></span></span></div>
            </div>
            <div class="mid" id="mid"></div>
            <div class="ctl">
                <div class="seg" id="views"></div>
                <div class="seg" id="ranges"></div>
            </div>
        </div>
        <div class="chart-wrap"><canvas id="invChart"></canvas></div>
        <div class="foot" id="foot"></div>

        <script>
            var VIEWS = ['REG', 'STACK', 'SHARE'];
            var RANGES = { '1M': 30, '3M': 91, 'ALL': null };
            var CHANGES = [1, 5, 20];
            var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
            var DAY = 86400000;
            var UP = '#4ade80', DOWN = '#f87171';

            Chart.defaults.font.family = "'JetBrains Mono', ui-monospace, Menlo, monospace";

            var rows = [];
            var view = 'REG';
            var range = '3M';
            var chart = null;

            function $(id) { return document.getElementById(id); }
            function isNum(v) { return typeof v === 'number' && isFinite(v); }
            function dayText(t) { var d = new Date(t); return MONTHS[d.getMonth()] + ' ' + d.getDate(); }
            function fullText(t) { return dayText(t) + ', ' + new Date(t).getFullYear(); }
            function mText(v, n) { return (v / 1e6).toFixed(n); }
            function pctText(v) { return v.toFixed(1) + '%'; }

            function lastIndex(list, key) {
                for (var i = list.length - 1; i >= 0; i--) { if (isNum(list[i][key])) return i; }
                return -1;
            }

            // Selected range: rows from the cutoff to the newest report, or everything when fewer than 2 registered values fall inside.
            function pickView() {
                var days = RANGES[range], last = rows[rows.length - 1].t;
                var min = days === null ? rows[0].t : last - days * DAY;
                var v = rows.filter(function (r) { return r.t >= min; });
                if (v.filter(function (r) { return isNum(r.reg); }).length < 2) { v = rows; min = rows[0].t; }
                return { view: v, min: min, max: last };
            }

            // Bounds rounded outward to the smallest step that leaves at most 5 intervals.
            function niceBounds(lo, hi, steps) {
                for (var i = 0; i < steps.length; i++) {
                    var s = steps[i], a = Math.floor(lo / s) * s, b = Math.ceil(hi / s) * s;
                    if (b === a) b = a + s;
                    if ((b - a) / s <= 5 || i === steps.length - 1) return { min: a, max: b, step: s };
                }
            }

            function span(list) {
                var ys = list.filter(isNum);
                return [Math.min.apply(null, ys), Math.max.apply(null, ys)];
            }

            function setText(id, text, color) {
                var el = $(id);
                el.textContent = text;
                el.style.color = color || '';
            }

            function line(text, color) {
                var s = document.createElement('span');
                s.textContent = text;
                s.style.color = color || '';
                var mid = $('mid');
                if (mid.children.length) mid.appendChild(document.createTextNode(' · '));
                mid.appendChild(s);
            }

            function renderHeader() {
                var n = rows.length - 1, r = rows[n];
                $('cap').textContent = 'REGISTERED · report ' + dayText(r.t);
                $('mid').textContent = '';
                if (!isNum(r.reg)) {
                    setText('score', '—');
                    setText('unit', '');
                    line('Latest report (' + dayText(r.t) + ') has no registered figure.');
                    return;
                }
                setText('score', mText(r.reg, 1) + 'M');
                setText('unit', ' oz');
                CHANGES.forEach(function (k) {
                    var p = rows[n - k];
                    if (!p || !isNum(p.reg) || p.reg <= 0) return;
                    var d = r.reg - p.reg, sign = d > 0 ? '+' : d < 0 ? '-' : '';
                    line(k + (k === 1 ? ' report ' : ' reports ') + sign + mText(Math.abs(d), 2) + 'M (' + sign + (Math.abs(d) / p.reg * 100).toFixed(1) + '%)',
                        d > 0 ? UP : d < 0 ? DOWN : '');
                });
            }

            function renderFooter(v) {
                var r = rows[rows.length - 1], parts = [];
                if (isNum(r.el)) parts.push('Eligible ' + mText(r.el, 1) + 'M');
                if (isNum(r.tot)) parts.push('Total ' + mText(r.tot, 1) + 'M');
                if (isNum(r.share)) parts.push('Registered share ' + pctText(r.share));
                parts.push(v.length + ' reports in view');
                var regs = v.filter(function (x) { return isNum(x.reg); });
                if (regs.length) {
                    var lo = regs[0], hi = regs[0];
                    regs.forEach(function (x) { if (x.reg < lo.reg) lo = x; if (x.reg > hi.reg) hi = x; });
                    parts.push('registered low ' + mText(lo.reg, 1) + 'M (' + dayText(lo.t) + ')');
                    parts.push('high ' + mText(hi.reg, 1) + 'M (' + dayText(hi.t) + ')');
                }
                setText('foot', parts.join(' · '));
            }

            function showMessage(text, color) {
                if (chart) { chart.destroy(); chart = null; }
                setText('foot', text, color);
            }

            var crosshair = {
                id: 'crosshair',
                beforeDatasetsDraw: function (c) {
                    var act = c.tooltip && c.tooltip.getActiveElements();
                    if (!act || !act.length) return;
                    var x = act[0].element.x, ctx = c.ctx;
                    ctx.save();
                    ctx.strokeStyle = '#3f3f46';
                    ctx.lineWidth = 1;
                    ctx.beginPath();
                    ctx.moveTo(x, c.chartArea.top);
                    ctx.lineTo(x, c.chartArea.bottom);
                    ctx.stroke();
                    ctx.restore();
                }
            };

            function series(v, key) {
                return v.map(function (r) { return { x: r.t, y: isNum(r[key]) ? r[key] : null }; });
            }

            function renderChart() {
                var p = pickView(), v = p.view, datasets, yb, fmt, stacked = view === 'STACK';
                var lastIdx = lastIndex(v, view === 'SHARE' ? 'share' : 'reg');
                function dot(c) { return c.dataIndex === lastIdx ? 4 : 0; }
                if (stacked) {
                    // a report missing either figure is missing in both layers
                    var ok = v.map(function (r) { return isNum(r.reg) && isNum(r.el); });
                    var top = v.map(function (r, i) { return ok[i] ? r.reg + r.el : null; });
                    yb = niceBounds(0, span(top)[1], [25e6, 50e6, 100e6, 200e6]);
                    yb.min = 0;
                    fmt = function (x) { return x === 0 ? '0' : mText(x, 0) + 'M'; };
                    datasets = [
                        { label: 'Registered', data: v.map(function (r, i) { return { x: r.t, y: ok[i] ? r.reg : null }; }), parsing: false, borderColor: '#f59e0b', backgroundColor: 'rgba(245,158,11,0.35)', fill: 'origin', borderWidth: 1, tension: 0, spanGaps: false, pointRadius: 0, pointHoverRadius: 3 },
                        { label: 'Eligible', data: v.map(function (r, i) { return { x: r.t, y: ok[i] ? r.el : null }; }), parsing: false, borderColor: '#38bdf8', backgroundColor: 'rgba(56,189,248,0.18)', fill: '-1', borderWidth: 1, tension: 0, spanGaps: false, pointRadius: 0, pointHoverRadius: 3 }
                    ];
                } else if (view === 'SHARE') {
                    var sb = span(v.map(function (r) { return r.share; }));
                    yb = niceBounds(sb[0], sb[1], [0.5, 1, 2, 5, 10, 20]);
                    fmt = function (x) { return x + '%'; };
                    datasets = [{ label: 'Registered share', data: series(v, 'share'), parsing: false, borderColor: '#e4e4e7', borderWidth: 1.75, tension: 0.15, spanGaps: false, pointRadius: dot, pointHoverRadius: 3, pointBackgroundColor: '#e4e4e7', pointBorderWidth: 0 }];
                } else {
                    var rb = span(v.map(function (r) { return r.reg; }));
                    yb = niceBounds(rb[0], rb[1], [0.5e6, 1e6, 2e6, 5e6, 10e6, 20e6]);
                    fmt = function (x) { return (Math.round(x / 1e5) / 10) + 'M'; };
                    datasets = [{ label: 'Registered', data: series(v, 'reg'), parsing: false, borderColor: '#f59e0b', backgroundColor: 'rgba(245,158,11,0.08)', fill: 'start', borderWidth: 1.75, tension: 0.15, spanGaps: false, pointRadius: dot, pointHoverRadius: 3, pointBackgroundColor: '#f59e0b', pointBorderWidth: 0 }];
                }
                if (chart) chart.destroy();
                chart = new Chart($('invChart').getContext('2d'), {
                    type: 'line',
                    data: { datasets: datasets },
                    plugins: [crosshair],
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        animation: false,
                        interaction: { mode: 'nearest', axis: 'x', intersect: false },
                        plugins: {
                            legend: { display: false },
                            tooltip: {
                                backgroundColor: 'rgba(9,9,11,0.95)', borderColor: '#27272a', borderWidth: 1, padding: 8, displayColors: false,
                                titleColor: '#a1a1aa', bodyColor: '#e4e4e7',
                                titleFont: { size: 10 }, bodyFont: { size: 11 },
                                filter: function (item, i) { return i === 0; },
                                callbacks: {
                                    title: function (items) { return fullText(items[0].parsed.x); },
                                    label: function (item) {
                                        // The rows come from the report at that x, whichever layer was hit.
                                        var r = v.find(function (q) { return q.t === item.parsed.x; });
                                        if (!r) return '';
                                        function oz(name, x) { return name + (isNum(x) ? ' ' + mText(x, 2) + 'M oz' : ' n/a'); }
                                        return [oz('Registered', r.reg), oz('Eligible', r.el), oz('Total', r.tot),
                                            'Registered share ' + (isNum(r.share) ? pctText(r.share) : 'n/a')];
                                    }
                                }
                            }
                        },
                        scales: {
                            x: {
                                type: 'linear', min: p.min, max: p.max, grid: { display: false }, border: { color: '#27272a' },
                                ticks: { color: '#71717a', font: { size: 9 }, maxRotation: 0, align: 'inner', callback: function (t) { return dayText(t); } },
                                afterBuildTicks: function (axis) {
                                    var step = Math.max(1, Math.ceil((axis.max - axis.min) / DAY / 5)) * DAY, ticks = [];
                                    for (var t = new Date(axis.max).setHours(0, 0, 0, 0); t >= axis.min; t -= step) ticks.unshift({ value: t });
                                    axis.ticks = ticks;
                                }
                            },
                            y: {
                                min: yb.min, max: yb.max, stacked: stacked, grid: { color: '#18181b' }, border: { display: false },
                                ticks: { color: '#71717a', font: { size: 9 }, stepSize: yb.step, callback: fmt }
                            }
                        }
                    }
                });
                renderFooter(v);
            }

            function makeButtons(id, names, isOn, onClick) {
                var box = $(id);
                box.textContent = '';
                names.forEach(function (name) {
                    var b = document.createElement('button');
                    b.type = 'button';
                    b.textContent = name;
                    b.setAttribute('aria-pressed', String(isOn(name)));
                    b.addEventListener('click', function () { onClick(name); });
                    box.appendChild(b);
                });
            }

            function refresh() {
                makeButtons('views', VIEWS, function (n) { return n === view; }, function (n) { view = n; refresh(); });
                makeButtons('ranges', Object.keys(RANGES), function (n) { return n === range; }, function (n) { range = n; refresh(); });
                renderChart();
            }

            function noHistory() {
                setText('score', '—');
                setText('unit', '');
                showMessage('No COMEX inventory history recorded yet.');
            }

            async function loadChart() {
                try {
                    var response = await fetch('/api/inventory_data');
                    var d = await response.json();
                    if (!response.ok || d.status === 'error' || !Array.isArray(d.labels)) {
                        showMessage('Could not load the COMEX inventory history.' + (d && d.message ? ': ' + d.message : ''), '#f87171');
                        return;
                    }
                    rows = d.labels.map(function (l, i) {
                        var q = l.split('-'), r = { t: new Date(+q[0], +q[1] - 1, +q[2]).getTime() };
                        function num(a) { var x = a && a[i]; return isNum(x) ? x : null; }
                        r.reg = num(d.registered); r.el = num(d.eligible); r.tot = num(d.total);
                        r.share = isNum(r.reg) && isNum(r.tot) && r.tot > 0 ? r.reg / r.tot * 100 : null;
                        return r;
                    });
                    if (!rows.length || lastIndex(rows, 'reg') < 0) { noHistory(); return; }
                    renderHeader();
                    refresh();
                } catch (e) {
                    console.error('Inventory chart failed:', e);
                    showMessage('Could not load the COMEX inventory history.', '#f87171');
                }
            }

            loadChart();
        </script>
    </body>
    </html>
    """
    return render_template_string(html_template)

# --- HELPER: NATIVE XML WHALE HUNT ---
SCAN_PRESETS = {
    # strategy: (max_dte, min_vol_oi, min_premium). max_dte 0 means no limit.
    "MORNING_HUNT": (14, 1.5, 100000.0),
    "EVENING_HUNT": (0, 1.0, 500000.0),
}


def execute_xml_whale_hunt(ticker, min_vol_oi=1.0, max_dte=30, strategy="CUSTOM_HUNT", min_premium=100000.0):
    """Fetches option chains and returns a structured XML object. Raises ApiError (XML) when the ticker has no
    options; any other failure propagates to the route's `guarded()` wrapper."""
    root = ET.Element("whale_hunt", ticker=ticker.upper(), strategy=strategy)

    tk = yf.Ticker(ticker)
    exps = tk.options
    if not exps:
        raise ApiError(f"No options found for {ticker.upper()}.", 404, xml=True)

    today = datetime.now()
    whale_list = []

    # Iterate through expirations
    for exp in exps:
        exp_date = datetime.strptime(exp, '%Y-%m-%d')
        days_to_exp = (exp_date - today).days

        # Apply Max DTE filter (0 = no limit)
        if max_dte and days_to_exp > int(max_dte):
            continue

        opt = tk.option_chain(exp)
        # Combine calls and puts into one list for processing
        for df, label in [(opt.calls, "CALL"), (opt.puts, "PUT")]:
            if df.empty: continue

            # Calculate metrics
            df['premium_spent'] = df['volume'] * df['lastPrice'] * 100
            df['vol_oi_ratio'] = df['volume'] / df['openInterest'].replace(0, 1)
            df['spread'] = df['ask'] - df['bid']

            # Filter: Vol/OI ratio and the premium floor
            mask = (df['vol_oi_ratio'] >= float(min_vol_oi)) & (df['premium_spent'] >= float(min_premium))
            whales = df[mask].copy()

            for _, row in whales.iterrows():
                whale_list.append({
                    "symbol": row['contractSymbol'],
                    "type": label,
                    "expiration": exp,
                    "strike": str(row['strike']),
                    "last_price": f"${row['lastPrice']:.2f}",
                    "bid": f"${row['bid']:.2f}",
                    "ask": f"${row['ask']:.2f}",
                    "spread": f"${row['spread']:.2f}",
                    "volume": str(int(row['volume'])),
                    "open_interest": str(int(row['openInterest'])),
                    "vol_oi_ratio": f"{row['vol_oi_ratio']:.2f}x",
                    "implied_volatility": f"{row['impliedVolatility']*100:.2f}%",
                    "premium_spent": f"${row['premium_spent']:,.2f}",
                    "raw_premium": row['premium_spent'] # For sorting
                })

    # Sort all found whales by premium spent (Highest first)
    whale_list.sort(key=lambda x: x['raw_premium'], reverse=True)
    root.set("whale_count", str(len(whale_list)))

    # Build the XML tree
    for w in whale_list:
        contract = ET.SubElement(root, "contract")
        for attr in ['symbol', 'type', 'expiration', 'strike', 'last_price',
                     'bid', 'ask', 'spread', 'volume', 'open_interest',
                     'vol_oi_ratio', 'implied_volatility', 'premium_spent']:
            contract.set(attr, w[attr])

    return root


def _whale_hunt_response(root):
    pretty_xml = minidom.parseString(ET.tostring(root, encoding='utf-8')).toprettyxml(indent="  ")
    return Response(pretty_xml, mimetype='application/xml')


def _run_preset_scan(strategy):
    """The morning and evening scans: the same filtered scan and XML as /api/custom with fixed thresholds."""
    ticker = ticker_arg(xml=True)
    max_dte, min_vol_oi, min_premium = SCAN_PRESETS[strategy]
    return _whale_hunt_response(execute_xml_whale_hunt(ticker, min_vol_oi, max_dte, strategy, min_premium))


@app.route('/api/morning', methods=['GET'])
@guarded("Morning scan", 502, xml=True)
def api_morning():
    """Short-dated unusual activity: expiring within 14 days, Vol/OI 1.5 or more, premium $100,000 or more."""
    return _run_preset_scan("MORNING_HUNT")


@app.route('/api/evening', methods=['GET'])
@guarded("Evening scan", 502, xml=True)
def api_evening():
    """Large positioning in any expiration: Vol/OI 1.0 or more, premium $500,000 or more."""
    return _run_preset_scan("EVENING_HUNT")


# --- THE ROUTE ---
@app.route('/api/custom', methods=['GET'])
@guarded("Custom scan", 502, xml=True)
def api_custom():
    ticker = ticker_arg(xml=True)
    # An empty field uses the default; an empty max_dte means no limit.
    min_vol_oi = float_arg('min_vol_oi', 1.0, minimum=0, xml=True)
    max_dte = int_arg('max_dte', 365, minimum=0, xml=True, blank=0)
    min_premium = float_arg('min_premium', 100000.0, minimum=0, xml=True)
    return _whale_hunt_response(execute_xml_whale_hunt(ticker, min_vol_oi, max_dte, "CUSTOM_HUNT", min_premium))

@app.route('/', methods=['GET'])
def serve_terminal():
    """Serves the Market Terminal UI."""
    return render_template('terminal.html')
    
@app.route('/help', methods=['GET'])
def api_help():
    """Outputs the complete API documentation in XML format."""
    help_xml = """<?xml version="1.0" ?>
<api_documentation>
  <reference>Full reference with response shapes, error behavior and known issues: docs/api.md in the repository.</reference>
  <notes>
    <note>There is no authentication. Keep the server on a private network.</note>
    <note>source="stored" reads pipeline output (SQLite, CSV ledgers, daily files). source="live" makes network calls while the request is open.</note>
    <note>Failures use a real HTTP status. JSON routes answer {"status": "error", "message": ...}; XML routes answer an error element. Messages are short; details are in the server log.</note>
    <note>A value a live route cannot compute is null, with the reason in the "missing" object of the data (time_arbitrage, option_calc). It is never replaced by a stand-in number.</note>
    <note>Routes that write something or start a process are POST or DELETE. A POST or DELETE that carries an Origin or Referer for a different host is refused with 403. There is no CORS: other sites cannot read the responses.</note>
  </notes>

  <group name="Terminal and system">
    <endpoint method="GET" path="/" format="html">
      <description>The web terminal (templates/terminal.html).</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/help" format="xml">
      <description>This page.</description>
      <parameters />
    </endpoint>
    <endpoint method="POST" path="/run" format="json" writes="pipeline run">
      <description>Starts a full pipeline run (run_dashboard.command with the manual trigger, under zsh) in the background and answers at once. 202 {"status": "started", "pid"}. 409 {"status": "busy", "run_id", "stage"} when a run already holds the run lock. 404 if the script is missing. Output goes to .v2_manual_run.log in the data folder; follow progress with /api/run_status.</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/api/run_status" format="json" source="stored">
      <description>State of the pipeline run from .v2_run_status.json: run_id, stage, state, pid, mode, started_at, updated_at, finished_at, elapsed_s, error (null when there is no file), plus lock_held. state is running, completed, completed_with_warnings or failed, or interrupted when the file says running but no run holds the lock.</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/api/dump" alias="/dump" format="xml" source="stored">
      <description>The tactical_ruling.txt and volume_dashboard.txt of the newest daily run folder (folders named like Sep-29-26, newest by the date in the name) merged into one XML document.</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/static/{filename}" format="file">
      <description>Terminal JavaScript and CSS (options_whale/static).</description>
      <parameters />
    </endpoint>
  </group>

  <group name="Macro and VMRI">
    <endpoint method="GET" path="/vmri" format="xml" source="stored">
      <description>The VMRI block from the newest tactical_ruling.txt, with the formulas and the four risk ranges.</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/api/vmri_history" format="json" source="stored">
      <description>The last 500 rows of the macro ledger: VMRI score, 10-period SMA, 5-period momentum, primary driver, the MMRI series (mmri, mmri_sma_10, mmri_momentum_5: DXY x 10Y yield / 1.61, recomputed from the stored columns) and context series (DXY, 10Y yield, VIX, gold, gold/silver ratio, HY OAS). Failures are {"status": "error", "message", "error"} with 404 or 500.</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/vmri_chart" format="html" source="stored">
      <description>VMRI chart page (loads /api/vmri_history).</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/mmri_chart" format="html" source="stored">
      <description>MMRI chart page: the Mannarino Market Risk Indicator (credit: Gregory Mannarino) with his four bands, drawn from the mmri series of /api/vmri_history.</description>
      <parameters />
    </endpoint>
    <endpoint method="GET,POST" path="/api/war_room" format="json" source="stored">
      <description>VMRI scenario: applies shifts to the latest ledger values and returns the current and hypothetical VMRI, tier, per-lever impact and where the scores sit in the ledger history. Parameters come from the query string (GET) or a JSON body (POST). 503 naming the ledger column when the macro ledger has no value for DXY, 10Y_Yield, High_Yield_OAS, VIX or VMRI_Score; 400 for a non-numeric parameter.</description>
      <parameters>
        <param name="dxy_shift" type="float" default="0" description="Added to DXY" />
        <param name="tnx_shift" type="float" default="0" description="Added to the 10Y yield" />
        <param name="oas_shift" type="float" default="0" description="Added to HY OAS" />
        <param name="vix_shift_pct" type="float" default="0" description="VIX change in percent; takes priority when not 0" />
        <param name="vix_shift" type="float" default="0" description="VIX change in points; used when vix_shift_pct is 0" />
      </parameters>
    </endpoint>
    <endpoint method="GET" path="/api/macro_calendar" format="json" source="stored">
      <description>Upcoming macro events (date, time, impact, title, forecast, previous) from the newest tactical_ruling.txt.</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/api/macro_news" format="xml" source="stored">
      <description>Up to 30 de-duplicated headlines from the newest snapshot's news section (RSS feeds collected by the pipeline), newest first and always including the newest 10 from official sources, each with a VADER sentiment score, its feed names and an official flag; the root carries the run time and how many feeds answered.</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/api/macro_ledger_full" format="json" source="stored">
      <description>Macro ledger series (VMRI, rates, volatility, commodities, silver and liquidity columns) as parallel arrays.</description>
      <parameters>
        <param name="limit" type="int" default="200" description="Number of most recent rows; a non-integer or a value below 1 is a 400" />
      </parameters>
    </endpoint>
  </group>

  <group name="Options and flow">
    <endpoint method="GET" path="/api/gex" format="json" source="live">
      <description>Black-Scholes gamma exposure by strike from the three nearest expirations, within 10 percent of spot, computed with the same function as the pipeline (core/metrics.py gex_profile). zeroGamma is the spot level where aggregate net GEX changes sign; it is null with zeroGammaReason when there is no sign change within 10 percent of spot.</description>
      <parameters>
        <param name="ticker" type="string" default="SPY" />
      </parameters>
    </endpoint>
    <endpoint method="GET" path="/api/darkpool" format="json" source="live">
      <description>Block trades (size 10,000 or more) in the last completed trading session from Databento: volume, notional, VWAP, bias and the 5 latest prints. Databento side B is a buy aggressor and A a sell aggressor, as in the pipeline (core/metrics.py block_flow). sentiment.method is aggressor, or vwap_heuristic when fewer than half of the block volume has a known side. Print sides are BUY, SELL or UNKNOWN. Needs a Databento key.</description>
      <parameters>
        <param name="ticker" type="string" default="SPY" />
      </parameters>
    </endpoint>
    <endpoint method="GET" path="/api/institutional_history" format="json" source="stored">
      <description>Dark pool and GEX history for one ticker from the equities_darkpool_gex_ledger.csv ledger. Five more arrays aligned with labels come from the pipeline database and are null where a row has no stored run: dp_session (the trading session the flow describes), dp_method (aggressor or vwap_heuristic), dp_unknown_vol (block volume with no provider side), dp_above_vwap_vol and dp_below_vwap_vol (the unknown-side volume at or above, and below, the block VWAP).</description>
      <parameters>
        <param name="ticker" type="string" default="SLV" />
        <param name="limit" type="int" default="100" description="Number of most recent rows; a non-integer or a value below 1 is a 400" />
      </parameters>
    </endpoint>
    <endpoint method="GET" path="/api/morning" format="xml" source="live">
      <description>Filtered scan of one ticker, same XML as /api/custom with strategy MORNING_HUNT: expirations within 14 days, Vol/OI of at least 1.5, premium of at least $100,000. No phone alert is sent.</description>
      <parameters>
        <param name="ticker" type="string" default="SPY" />
      </parameters>
    </endpoint>
    <endpoint method="GET" path="/api/evening" format="xml" source="live">
      <description>Filtered scan of one ticker, same XML as /api/custom with strategy EVENING_HUNT: any expiration, Vol/OI of at least 1.0, premium of at least $500,000. No phone alert is sent.</description>
      <parameters>
        <param name="ticker" type="string" default="SPY" />
      </parameters>
    </endpoint>
    <endpoint method="GET" path="/api/custom" format="xml" source="live">
      <description>Unusual-activity scan of the option chains. Keeps contracts with volume x last price x 100 of at least min_premium and Vol/OI at or above min_vol_oi, expiring within max_dte days. Calls and puts, in or out of the money. An empty parameter uses its default. No options for the ticker is a 404 error element; a provider failure is a 502.</description>
      <parameters>
        <param name="ticker" type="string" default="SPY" />
        <param name="min_vol_oi" type="float" default="1.0" description="Minimum volume / open interest" />
        <param name="max_dte" type="int" default="365" description="Maximum days to expiration; 0 or empty means no limit" />
        <param name="min_premium" type="float" default="100000" description="Minimum premium in dollars (volume x last price x 100)" />
      </parameters>
    </endpoint>
    <endpoint method="GET" path="/api/option_chain" format="json" source="live">
      <description>Calls and puts (strike, last, bid, ask, iv, oi, volume) for one expiration, plus the first 24 expirations and the spot price.</description>
      <parameters>
        <param name="ticker" type="string" default="SPY" />
        <param name="expiration" type="date" default="first expiration after today" description="YYYY-MM-DD; ignored if not listed" />
      </parameters>
    </endpoint>
    <endpoint method="GET" path="/api/option_calc" format="json" source="live">
      <description>Black-Scholes price, Greeks and probability for one contract, using the live chain implied volatility of that contract. 422 when the chain has no implied volatility for it; 503 when the risk-free rate (^IRX) is unavailable; 400 for a missing strike or a bad expiration or type. hv_pct and iv_signal use the realized volatility of the requested ticker and are null, with a reason in data.missing, when Yahoo has too little history. market_price is null, with a reason in data.missing, when no price was given and the chain has no last price; breakeven then uses the Black-Scholes price.</description>
      <parameters>
        <param name="ticker" type="string" default="SPY" />
        <param name="strike" type="float" default="0" description="Required, above 0" />
        <param name="expiration" type="date" default="empty" description="YYYY-MM-DD; required" />
        <param name="type" type="string" default="call" description="call or put" />
        <param name="market_price" type="float" default="0" description="0 uses the last traded price from the chain" />
      </parameters>
    </endpoint>
    <endpoint method="GET" path="/api/time_arbitrage" format="json" source="live">
      <description>Options analytics for a ticker: z-score oscillator (z_components lists the factors it used), gamma state, vanna and charm, IV bleed, strike probabilities, IV term structure and IV/HV spread. IV bleed, vanna and charm use the 15 call strikes nearest the spot price; probabilities use the 5 nearest strikes. Reads the macro and dark pool ledgers and calls Yahoo Finance. Anything it cannot compute is null, with the reason in data.missing.</description>
      <parameters>
        <param name="ticker" type="string" default="SPY" />
      </parameters>
    </endpoint>
  </group>

  <group name="Silver and arbitrage">
    <endpoint method="POST" path="/api/silver_eagle_prices" format="xml" source="live" writes="appends a row to physical_arbitrage_ledger">
      <description>Runs ebay.py: prices of tracked 1 oz Silver Eagle listings from the eBay Browse API, with premium over the benchmark, the silver futures price SI=F (the root attribute benchmark_symbol names it; comex_spot is the historical attribute name and holds that price). Each successful call appends a row to the arbitrage ledger (CSV and SQLite). Needs eBay credentials. POST only.</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/api/shanghai_spread" format="json" source="stored">
      <description>Shanghai Gold Exchange silver benchmark against COMEX silver futures from the latest snapshot: legacy (the fields as stored, comparing the latest fix with the run-time quote) and dated (each fix paired with the SI=F and USD/CNY closes of its own date, with 90-day statistics and a daily series; null when the snapshot predates it).</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/api/paper_physical" format="json" source="stored">
      <description>The latest snapshot's paper_physical section as stored: the ratio and tier, the open interest and registered stock behind it, and history (the recorded ratio's range, median, percentile and 30-day change; absent when the snapshot predates it).</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/api/arbitrage_history" format="json" source="stored">
      <description>Silver Eagle premium history, paired per run from macro_master_ledger.csv (the retail prices against the silver futures price recorded by the same run), as parallel arrays. The 90-day statistics (stats), the dealer book (book) and the units-sold flow (flow) are read from the latest run's snapshot; each is null when there is no database or snapshot yet, or the snapshot predates them.</description>
      <parameters>
        <param name="limit" type="int" default="50" description="Number of most recent rows; a non-integer or a value below 1 is a 400" />
      </parameters>
    </endpoint>
    <endpoint method="GET" path="/api/inventory_data" format="json" source="stored">
      <description>COMEX registered, eligible and total silver inventory by date from comex_inventory_history.csv.</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/inventory_chart" format="html" source="stored">
      <description>COMEX inventory chart page (loads /api/inventory_data).</description>
      <parameters />
    </endpoint>
  </group>

  <group name="Engine positions">
    <endpoint method="GET" path="/api/brokerage" format="json" source="live (SnapTrade, cached 60 s)" params="refresh=1">Broker stocks and option spreads; status not_configured carries the setup steps.</endpoint>
    <endpoint method="GET" path="/api/positions" format="json" source="stored and live">
      <description>Tracked engine positions from the database (opened read-only), with live underlying and option prices and projected values. 503 until a pipeline run has created the database.</description>
      <parameters />
    </endpoint>
    <endpoint method="POST" path="/api/positions/{signal_id}/star" format="json" writes="toggles starred">
      <description>Toggles the star on one position. 404 if the id is unknown.</description>
      <parameters />
    </endpoint>
    <endpoint method="DELETE" path="/api/positions/{signal_id}" format="json" writes="stops tracking">
      <description>Stops tracking a position (marks it deleted; the record is kept). 404 if the id is unknown or already stopped.</description>
      <parameters />
    </endpoint>
  </group>

  <group name="Forecast Lab">
    <endpoint method="GET" path="/api/forecast" format="json" source="stored and live">
      <description>The Forecast Lab cards of the latest committed snapshot plus the scorecard (graded with live daily closes). The database is opened read-only. 503 until a pipeline run has created the database; 404 when it holds no snapshot with forecast data.</description>
      <parameters>
        <param name="ticker" type="string" default="SPY" description="SPY or SLV; anything else returns 400" />
      </parameters>
    </endpoint>
    <endpoint method="GET" path="/api/chaos/history" format="json" source="stored">
      <description>The backdated daily Chaos / Peace score: the newest stored observation per day for the current model version, oldest first, as parallel arrays (t = days since 1970-01-01, score, used = components counted). 503 until a pipeline run has created the database; 404 until a run has stored the history.</description>
      <parameters>
        <param name="from" type="string" default="" description="First date to return, YYYY-MM-DD; a bad date is a 400" />
      </parameters>
    </endpoint>
    <endpoint method="GET" path="/api/eia_history" format="json" source="stored">
      <description>Weekly EIA stock and days-of-supply history behind Forecast Lab card 11, built from files the pipeline captured.</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/api/chaos" format="json" source="stored">
      <description>The Chaos / Peace block (Forecast Lab card 14) of the latest committed snapshot: score, regime, components, levers, election studies. 503 until a pipeline run has created the database; 404 when the snapshot has no chaos block.</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/api/fed_ops" format="json" source="stored">
      <description>Fed / Treasury operations (card 15) of the latest committed snapshot, or before the first run the same block summarised from the stored history (every part stale). repo: per market and reference rate (SOFR, TGCR, BGCR, EFFR) the latest day, the 4-week change and `ago` (the reading 1 month, 3 months and 1 year earlier); `monthly` and `yearly` levels (average, highest day, last day with its date, in USD bn; rate means). buybacks: `this_year_vs_last_year_same_dates`, the last operations, scheduled ones, the 5 largest, monthly, quarterly, yearly (with year to date and by operation type) and trailing-12-month totals, by security type. auctions: recent results, the upcoming calendar, per-tenor bid-to-cover now and a year earlier and by year, monthly issuance. A part that could not be refreshed is status stale with the date of its stored data. 404 when nothing is stored.</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/api/fed_ops/repo" format="json" source="stored">
      <description>The repo part of /api/fed_ops.</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/api/fed_ops/buybacks" format="json" source="stored">
      <description>The buybacks part of /api/fed_ops.</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/api/fed_ops/auctions" format="json" source="stored">
      <description>The auctions part of /api/fed_ops.</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/api/fed_ops/history/repo" format="json" source="stored">
      <description>Stored daily repo market history, one row per day: accepted USD bn for rrp, repo and repo_term, the sofr, tgcr, bgcr and effr rates and SOFR volume (from 2014; rates from 2018-04-02). Returns the newest `limit` rows in the range, oldest first, the earliest stored date and the repo summaries (yearly and monthly levels: average, highest day, last day with its date; rate means). The latest per market and rate is /api/fed_ops/repo. Same parameters for the two routes below.</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/api/fed_ops/history/buybacks" format="json" source="stored">
      <description>Every stored Treasury buyback operation since 2024 (max, offered and accepted USD bn) with the monthly, quarterly, yearly and trailing-12-month totals.</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/api/fed_ops/history/auctions" format="json" source="stored">
      <description>Every stored Treasury auction result since 2021 (size, high yield or bill rate, bid-to-cover, dealer / direct / indirect shares) with the per-tenor and monthly summaries.</description>
      <parameters>
        <param name="from" type="string" default="" description="first date, YYYY-MM-DD" />
        <param name="to" type="string" default="" description="last date, YYYY-MM-DD" />
        <param name="limit" type="int" default="60" description="newest rows kept (1 to 5000)" />
        <param name="tenor" type="string" default="" description="auctions: keep matching tenors; spacing and hyphens do not matter: 30 year, 30-Year, 30yr, 10 year note, bond, Bill, TIPS" />
        <param name="security_type" type="string" default="" description="buybacks: keep security types containing this text, e.g. TIPS" />
      </parameters>
    </endpoint>
  </group>

  <group name="Rule cards and console">
    <endpoint method="GET" path="/api/guide" format="json" source="stored">
      <description>The plain-English guide (docs/guide.md): the two sections that best match term, or every heading without one. status missing when nothing matches.</description>
      <parameters>
        <param name="term" type="string" default="" description="what to explain, e.g. reverse repo, bid-to-cover, VMRI" />
      </parameters>
    </endpoint>
    <endpoint method="GET" path="/api/scanner" format="json" source="stored and live">
      <description>Day Scanner (card 12): the dip-in-an-uptrend rule for every ticker on the saved watchlist, with live quotes, trigger prices and evidence, plus the live-watch paper positions. POST with {"symbol": "XYZ"} checks the ticker against Yahoo and adds it (400 with a reason if it is refused).</description>
      <parameters>
        <param name="refresh" type="string" default="" description="1 drops the cached price history, earnings dates and quotes first" />
      </parameters>
    </endpoint>
    <endpoint method="DELETE" path="/api/scanner/{symbol}" format="json" writes="removes a ticker from the watchlist">
      <description>Removes one ticker from the Day Scanner watchlist and returns the scanner again. SPY cannot be removed.</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/api/edges" format="json" source="live">
      <description>Edge Lab (card 13): published edges tested on each tracked ticker's own history. POST with {"symbol": "XYZ"} analyses the ticker and tracks it.</description>
      <parameters>
        <param name="symbol" type="string" default="" description="Analyse this ticker without saving it; the result is in `query`. An unknown ticker is a 400" />
        <param name="refresh" type="string" default="" description="1 drops the cached history first" />
      </parameters>
    </endpoint>
    <endpoint method="DELETE" path="/api/edges/{symbol}" format="json" writes="stops tracking a ticker">
      <description>Removes one ticker from the Edge Lab tracked list and returns the tracked analyses.</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/api/assistant/status" format="json" source="live">
      <description>Console engine v3: the model server address, whether it is reachable, its models, and the read-only data sources the model may use.</description>
      <parameters />
    </endpoint>
    <endpoint method="GET" path="/api/assistant/data" format="text" source="stored and live">
      <description>Exactly what the console's model sees for one data source, shaped and size-limited.</description>
      <parameters>
        <param name="source" type="string" default="" description="A source name from /api/assistant/status, or brief, or calc" />
        <param name="q" type="string" default="" description="Query string for the source (e.g. ticker=SPY), or the expression for calc" />
        <param name="path" type="string" default="" description="Dotted path to one part of the result" />
        <param name="last" type="int" default="10" description="How many items of long lists to keep" />
      </parameters>
    </endpoint>
    <endpoint method="POST" path="/api/assistant/ask" format="server-sent events">
      <description>Asks the console engine a question ({"question", "model", "conversation"}). Streams events: delta, replace, thinking, tool, error, done. The model can only read the listed GET sources.</description>
      <parameters />
    </endpoint>
    <endpoint method="POST" path="/api/assistant/reset" format="json" writes="forgets one conversation (in memory)">
      <description>Forgets the conversation with the given id.</description>
      <parameters />
    </endpoint>
  </group>
</api_documentation>"""
    
    return Response(help_xml, mimetype='application/xml')

# --- RUN CONTROL ---
RUN_STATUS_KEYS = ("run_id", "stage", "state", "pid", "mode", "started_at", "updated_at", "finished_at",
                   "elapsed_s", "error")
_run_start_lock = threading.Lock()
_manual_run = None   # the Popen of the last run this server started


def _read_run_status():
    """The fields of .v2_run_status.json (null for each one when the file is missing or unreadable)."""
    raw = runlock.read_status()
    return {key: raw.get(key) for key in RUN_STATUS_KEYS}


def _run_lock_held():
    try:
        return bool(runlock.lock_is_held())
    except OSError:
        return False


@app.route('/api/run_status', methods=['GET'])
def api_run_status():
    """State of the pipeline run: the status file plus whether a run holds the lock right now. A file that says
    "running" while nothing holds the lock is reported as "interrupted" (the run was killed)."""
    status = _read_run_status()
    held = _run_lock_held()
    if status["state"] == "running" and not held:
        status["state"] = "interrupted"
    status["lock_held"] = held
    return jsonify({"status": "success", "data": status})


@app.route('/run', methods=['POST'])
def run_dashboard():
    """Starts run_dashboard.command with the manual trigger in the background and answers at once (202).
    The run's output goes to .v2_manual_run.log in the data folder; its progress is on GET /api/run_status."""
    global _manual_run
    if not os.path.exists(RUN_COMMAND):
        return json_error("run_dashboard.command not found.", 404)

    with _run_start_lock:
        if _run_lock_held():
            status = _read_run_status()
            return jsonify({"status": "busy", "message": "a run is already in progress",
                            "run_id": status["run_id"], "stage": status["stage"]}), 409
        if _manual_run is not None and _manual_run.poll() is None:
            # started a moment ago and has not taken the lock yet
            return jsonify({"status": "busy", "message": "a run is already starting", "run_id": None,
                            "stage": "starting"}), 409
        try:
            if not os.path.isdir(DATA_DIR):
                if os.environ.get("PORTFOLIO_DATA_DIR"):
                    return json_error("data directory not found.", 503)
                os.makedirs(DATA_DIR, exist_ok=True)       # the default sibling CME_Data, as the pipeline does
            with open(MANUAL_RUN_LOG, "ab") as log:
                log.write(f"\n--- manual run requested {datetime.now().isoformat(timespec='seconds')} ---\n".encode())
                log.flush()
                proc = subprocess.Popen(["/bin/zsh", RUN_COMMAND, "manual"], cwd=str(_PROJECT_ROOT),
                                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                        start_new_session=True)
        except Exception as e:
            app.logger.error("Could not start the pipeline run: %s", e, exc_info=e)
            return json_error("could not start the run (see the server log)", 500)
        _manual_run = proc
        threading.Thread(target=proc.wait, daemon=True).start()     # reap the child when it ends
    return jsonify({"status": "started", "pid": proc.pid}), 202

@app.route('/api/dump', methods=['GET'])
@app.route('/dump', methods=['GET'])
def dump_data():
    """Finds the most recent daily run folder and intelligently merges XML files."""
    folders = _run_folders()
    if folders is None:
        return xml_error("Data directory not found.", 404)

    # 1. Locate the latest daily folder (by the date in its name)
    if not folders:
        return xml_error("No daily run folders found.", 404)
    latest_folder = folders[0]

    # 2. Setup paths
    tactical_path = os.path.join(latest_folder, "tactical_ruling.txt")
    volume_path = os.path.join(latest_folder, "volume_dashboard.txt")

    # 3. Create Master Container
    root = ET.Element("cme_data_dump", source_folder=os.path.basename(latest_folder), timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    # Helper to parse and strip headers
    def get_parsed_xml(path):
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
                clean = re.sub(r'<\?xml[^>]*\?>', '', content).strip()
                try:
                    return ET.fromstring(clean)
                except ET.ParseError:
                    return None
        return None

    # 4. INTELLECTUALLY MERGE
    tac_xml = get_parsed_xml(tactical_path)
    vol_xml = get_parsed_xml(volume_path)

    # If the tactical ruling exists standalone, add it first
    if tac_xml is not None:
        root.append(tac_xml)

    if vol_xml is not None:
        # CHECK: Is there a redundant tactical ruling inside the dashboard?
        # If so, remove it from the dashboard block so we don't have double data
        redundant_node = vol_xml.find("tactical_ruling")
        if redundant_node is not None and tac_xml is not None:
            vol_xml.remove(redundant_node)
            
        root.append(vol_xml)

    # 5. Generate Pretty Output
    raw_xml = ET.tostring(root, encoding='utf-8')
    xml_str = minidom.parseString(raw_xml).toprettyxml(indent="  ")
    
    # Remove empty lines for a tighter output
    xml_str = os.linesep.join([s for s in xml_str.splitlines() if s.strip()])

    return Response(xml_str, mimetype='application/xml')

@app.route('/vmri', methods=['GET'])
def get_vmri():
    """Extracts the latest VMRI score and outputs it with full documentation and formulas."""
    subdirs = _run_folders()      # daily run folders, newest first by the date in the name
    if subdirs is None:
        return xml_error("Data directory not found.", 404)
    if not subdirs:
        return xml_error("No daily run folders found.", 404)

    latest_folder = None
    tactical_content = ""

    # Hunt for the tactical ruling file
    for folder in subdirs:
        tactical_path = os.path.join(folder, "tactical_ruling.txt")
        if os.path.exists(tactical_path):
            latest_folder = folder
            with open(tactical_path, 'r', encoding='utf-8') as f:
                tactical_content = f.read()
            break

    if not tactical_content:
        return xml_error("tactical_ruling.txt not found in recent folders.", 404)

    clean_content = re.sub(r'<\?xml[^>]*\?>', '', tactical_content).strip()
    
    try:
        tactical_tree = ET.fromstring(clean_content)
        vmri_node = tactical_tree.find(".//VLAD_MACRO_RISK_INDEX")
        
        if vmri_node is None:
            return xml_error("VMRI data not found inside the latest tactical ruling.", 404)

        root = ET.Element("vmri_report", timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'), source_folder=os.path.basename(latest_folder))
        
        # 1. Attach Live Data
        live_data = ET.SubElement(root, "live_calculation")
        live_data.append(vmri_node)

        # 2. Attach Top-Level Formula
        formula_node = ET.SubElement(root, "master_formula")
        formula_node.text = "VMRI Score = Base Stress * Credit Multiplier * Volatility Premium"
        
        # 3. Attach Documentation
        doc_node = ET.SubElement(root, "documentation")
        
        desc = ET.SubElement(doc_node, "description")
        desc.text = "The Vlad Macro Risk Index (VMRI) aggregates fixed income stress, credit spreads, and equity volatility into a single numerical threat level to dictate portfolio hedging aggression."
        
        # --- NEW: Mechanics Breakdown with Formulas ---
        mechanics_doc = ET.SubElement(doc_node, "mechanics_breakdown")
        
        # Base Stress
        bs = ET.SubElement(mechanics_doc, "metric", name="Base Stress")
        bs_desc = ET.SubElement(bs, "description")
        bs_desc.text = "The dollar and rates component: the US Dollar Index (DXY) times the 10-Year Treasury yield, scaled by 1.61."
        bs_form = ET.SubElement(bs, "formula")
        bs_form.text = "(DXY * 10Y Yield) / 1.61"
        
        # Credit Multiplier
        cm = ET.SubElement(mechanics_doc, "metric", name="Credit Multiplier")
        cm_desc = ET.SubElement(cm, "description")
        cm_desc.text = "A scaling factor based on High Yield OAS spreads. Values < 1.0 mean credit is healthy and dampens risk. Values > 1.0 indicate widening credit spreads, amplifying the systemic threat."
        cm_form = ET.SubElement(cm, "formula")
        cm_form.text = "Current HY OAS / 4.00"
        cm_base = ET.SubElement(cm, "baseline")
        cm_base.text = "4.00%"
        
        # Volatility Premium
        vp = ET.SubElement(mechanics_doc, "metric", name="Volatility Premium")
        vp_desc = ET.SubElement(vp, "description")
        vp_desc.text = "An accelerator based on equity derivatives (VIX). Values > 1.0 mean options markets are pricing in severe near-term turbulence, driving up the final score."
        vp_form = ET.SubElement(vp, "formula")
        vp_form.text = "Current VIX / 20.00"
        vp_base = ET.SubElement(vp, "baseline")
        vp_base.text = "20.00"
        
        # Ranges
        ranges = ET.SubElement(doc_node, "ranges")
        ET.SubElement(ranges, "level", range="0 - 150", status="LOW RISK", action="Maximize long exposure. Volatility is suppressed.")
        ET.SubElement(ranges, "level", range="150 - 250", status="MODERATE RISK", action="Normal market conditions. Standard position sizing.")
        ET.SubElement(ranges, "level", range="250 - 350", status="ELEVATED RISK", action="Hedge triggers active. Reduce beta, increase cash.")
        ET.SubElement(ranges, "level", range="350+", status="SYSTEMIC THREAT", action="Liquidity event probable. Maximum defensive posture.")

        # Generate pretty XML
        xml_str = minidom.parseString(ET.tostring(root, encoding='utf-8')).toprettyxml(indent="  ")
        xml_str = os.linesep.join([s for s in xml_str.splitlines() if s.strip()]) 

        return Response(xml_str, mimetype='application/xml')

    except Exception as e:
        app.logger.error("VMRI report failed: %s", e, exc_info=e)
        return xml_error("Failed to parse the VMRI data (see the server log).", 500)

@app.route('/api/macro_news', methods=['GET'])
@guarded("Macro news", 502, xml=True)
def api_macro_news():
    """Headlines of the latest snapshot's `news` section (collected by the pipeline from the RSS registry)."""
    conn = _open_readonly()
    if conn is None:
        return xml_error("database not initialised: run the pipeline once", 503)
    try:
        row = conn.execute("SELECT run_id FROM v2_latest_snapshot").fetchone()
        ctx = _v2lake.load_snapshot(conn, row["run_id"]) if row else {}
    except sqlite3.OperationalError as exc:
        if _uninitialised(exc):
            return xml_error("database not initialised: run the pipeline once", 503)
        raise
    finally:
        conn.close()
    news = ctx.get("news") or {}
    if news.get("status") not in ("fresh", "partial") or not news.get("clusters"):
        return xml_error("Macro news unavailable: " + (news.get("reason") or "no news section in the latest snapshot; run the pipeline"), 503)
    cov = news.get("coverage") or {}
    root = ET.Element("macro_news", timestamp=news.get("as_of") or "", as_of=news.get("as_of") or "", status=news["status"],
                      feeds_answered=str(cov.get("answered")), feeds_enabled=str(cov.get("enabled")), source="stored")
    official = [c for c in news["clusters"] if c.get("official")][:10]        # never crowded out by a busy wire day
    shown = sorted(official + [c for c in news["clusters"] if not c.get("official")][:30 - len(official)],
                   key=lambda c: c["published_at"], reverse=True)
    for c in shown:
        pub = datetime.fromisoformat(c["published_at"]).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        ET.SubElement(root, "article", published=pub, title=c["title"], link=c["sources"][0]["url"], sentiment=str(c["sentiment"]),
                      source=",".join(dict.fromkeys(u["name"] for u in c["sources"])), official=str(bool(c.get("official"))).lower())
    xml_str = minidom.parseString(ET.tostring(root, encoding='utf-8')).toprettyxml(indent="  ")
    return Response(os.linesep.join([ln for ln in xml_str.splitlines() if ln.strip()]), mimetype='application/xml')

# ==========================================
# --- INSTITUTIONAL FLOW HISTORY API ---
# ==========================================

def _utc_to_local_label(stamp):
    """A stored UTC timestamp as the ledger's 'YYYY-MM-DD HH:MM' in the configured local zone (None if unparseable).
    Must produce the same string as the `stamp` in core/render.py `ledger_rows()`: rows are matched to runs by it."""
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(config.LOCAL_TZ).strftime('%Y-%m-%d %H:%M')


def _flow_side_info(conn, ticker):
    """{ledger row date: {session, method, unknown_vol, above_vwap_vol, below_vwap_vol}} from the pipeline database.
    Row date -> run (run.generated_at in local time) -> session (flow.<T>.observed_session) -> summary
    (flow.<T>.summary, newest per session). A missing link leaves the later fields None."""
    runs = conn.execute("""SELECT o.run_id, o.observed_at FROM v2_observations o JOIN v2_runs r ON r.run_id = o.run_id
                           WHERE o.metric_id = 'run.generated_at' AND r.mode = 'live'
                             AND r.status IN ('committed', 'completed', 'completed_with_warnings')""").fetchall()
    sessions = {r["run_id"]: r["value_text"] for r in conn.execute(
        "SELECT run_id, value_text FROM v2_observations WHERE metric_id = ?", (f"flow.{ticker}.observed_session",))}
    summaries = {}
    for r in conn.execute("SELECT value_json FROM v2_observations WHERE metric_id = ? ORDER BY obs_id",
                          (f"flow.{ticker}.summary",)):
        try:
            summary = json.loads(r["value_json"] or "null")
        except ValueError:
            continue
        if isinstance(summary, dict) and summary.get("session"):
            summaries[summary["session"]] = summary
    out = {}
    for run in runs:
        label, session = _utc_to_local_label(run["observed_at"]), sessions.get(run["run_id"])
        if label is None or not session:
            continue
        summary = summaries.get(session) or {}
        split = summary.get("vwap_heuristic_unknown_side") or {}
        out[label] = {"session": session, "method": summary.get("bias_method"),
                      "unknown_vol": summary.get("unknown_side_volume"),
                      "above_vwap_vol": split.get("at_or_above_vwap"), "below_vwap_vol": split.get("below_vwap")}
    return out


@app.route('/api/institutional_history')
def get_institutional_history():
    """Returns time-series data from the institutional scanner ledger (SPY + SLV dark pool + GEX)."""
    ticker = (request.args.get('ticker') or 'SLV').upper()
    limit = int_arg('limit', 100)
    
    try:
        if not os.path.exists(INSTITUTIONAL_LEDGER_CSV):
            return jsonify({"status": "error", "message": "Institutional ledger not found."}), 404
        
        df = data_cache.get_csv(INSTITUTIONAL_LEDGER_CSV)
        
        if df.empty:
            return jsonify({"status": "error", "message": "Ledger is empty."}), 404
        
        # Filter by ticker (SPY or SLV)
        df = df[df['Ticker'].str.upper() == ticker].copy()
        
        if df.empty:
            return jsonify({"status": "error", "message": f"No data found for {ticker}."}), 404
        
        df = df.tail(limit)
        
        # Clean NaN values for JSON serialization
        df = df.astype(object).where(pd.notnull(df), None)
        
        # Build the payload
        payload = {
            "ticker": ticker,
            "labels": df['Date'].tolist(),
            "spot_price": df['Spot_Price'].tolist(),
            "dp_sentiment": df['DP_Sentiment'].tolist(),
            "dp_total_vol": df['DP_Total_Vol'].tolist(),
            "dp_notional": df['DP_Notional_USD'].tolist(),
            "dp_largest_block": df['DP_Largest_Block'].tolist(),
            "dp_vwap": df['DP_VWAP'].tolist(),
            "dp_bull_vol": df['DP_Bull_Vol'].tolist(),
            "dp_bear_vol": df['DP_Bear_Vol'].tolist(),
            "gex_call_wall": df['GEX_Call_Wall'].tolist(),
            "gex_put_wall": df['GEX_Put_Wall'].tolist(),
            "gex_zero_gamma": df['GEX_Zero_Gamma'].tolist()
        }

        # How the bias was reached, from the pipeline database (all null when it is not there yet).
        side = {}
        conn = _open_readonly()
        if conn is not None:
            try:
                side = _flow_side_info(conn, ticker)
            except sqlite3.OperationalError as exc:
                if not _uninitialised(exc):
                    raise
            finally:
                conn.close()
        for key, field in (("dp_session", "session"), ("dp_method", "method"), ("dp_unknown_vol", "unknown_vol"),
                           ("dp_above_vwap_vol", "above_vwap_vol"), ("dp_below_vwap_vol", "below_vwap_vol")):
            payload[key] = [(side.get(str(d)) or {}).get(field) for d in payload["labels"]]

        return jsonify({"status": "success", "data": payload})
    
    except Exception as e:
        app.logger.error("Institutional history failed: %s", e, exc_info=e)
        return json_error("Institutional history failed (see the server log).", 500)

# ==========================================
# --- MACRO CALENDAR API ---
# ==========================================

@app.route('/api/macro_calendar')
def get_macro_calendar():
    """Extracts upcoming macro catalyst events from the latest tactical_ruling.txt XML."""
    try:
        # Find the latest daily folder (newest by the date in its name) with a tactical_ruling.txt
        subdirs = _run_folders()
        if subdirs is None:
            return json_error("Data directory not found.", 404)

        tactical_content = None
        for folder in subdirs:
            tac_path = os.path.join(folder, "tactical_ruling.txt")
            if os.path.exists(tac_path):
                with open(tac_path, 'r', encoding='utf-8') as f:
                    tactical_content = f.read()
                break
        
        if not tactical_content:
            return jsonify({"status": "error", "message": "No tactical_ruling.txt found."}), 404
        
        # Parse the XML
        clean_content = re.sub(r'<\?xml[^>]*\?>', '', tactical_content).strip()
        root = ET.fromstring(clean_content)
        
        events = []
        calendar_node = root.find(".//upcoming_macro_events")
        
        if calendar_node is not None:
            for event in calendar_node.findall("event"):
                events.append({
                    "date": event.get("date", ""),
                    "time": event.get("time", ""),
                    "impact": event.get("impact", ""),
                    "title": event.get("title", ""),
                    "forecast": event.get("forecast", ""),
                    "previous": event.get("previous", "")
                })
        
        return jsonify({"status": "success", "events": events})
    
    except Exception as e:
        app.logger.error("Macro calendar failed: %s", e, exc_info=e)
        return json_error("Macro calendar failed (see the server log).", 500)

# ==========================================
# --- FULL MACRO LEDGER API ---
# ==========================================

@app.route('/api/macro_ledger_full')
def get_macro_ledger_full():
    """Returns all 25 columns from the macro master ledger for full overlay charting."""
    limit = int_arg('limit', 200)
    
    try:
        if not os.path.exists(LEDGER_CSV):
            return jsonify({"status": "error", "message": "Macro ledger not found."}), 404
        
        df = data_cache.get_csv(LEDGER_CSV)
        df = df.dropna(how='all')
        df = df.dropna(subset=['Datetime'])
        df['Datetime'] = pd.to_datetime(df['Datetime'], format='mixed')
        df = df.dropna(subset=['Datetime'])
        df = df.sort_values('Datetime').tail(limit)
        
        labels = df['Datetime'].dt.strftime('%Y-%m-%d %H:%M').tolist()
        
        def safe_list(col_name):
            """Convert a column to a clean list, replacing NaN/inf with None."""
            if col_name not in df.columns:
                return [None] * len(df)
            vals = []
            for val in df[col_name].tolist():
                if pd.isna(val) or val in ["NaN", "nan", "None", ""]:
                    vals.append(None)
                else:
                    try:
                        f = float(val)
                        if math.isnan(f) or math.isinf(f):
                            vals.append(None)
                        else:
                            vals.append(f)
                    except (ValueError, TypeError):
                        vals.append(None)
            return vals
        
        payload = {
            "labels": labels,
            "vmri_score": safe_list("VMRI_Score"),
            "threat_tier": df.get("Threat_Tier", pd.Series(dtype=str)).fillna("N/A").tolist(),
            "dxy": safe_list("DXY"),
            "dxy_change": safe_list("DXY_Change"),
            "ten_y_yield": safe_list("10Y_Yield"),
            "zn_futures": safe_list("ZN_Futures"),
            "high_yield_oas": safe_list("High_Yield_OAS"),
            "vix": safe_list("VIX"),
            "vix_change": safe_list("VIX_Change"),
            "wti_crude": safe_list("WTI_Crude"),
            "brent_crude": safe_list("Brent_Crude"),
            "gold_price": safe_list("Gold_Price"),
            "gold_silver_ratio": safe_list("Gold_Silver_Ratio"),
            "shfe_silver_usd": safe_list("SHFE_Silver_USD"),
            "comex_silver": safe_list("COMEX_Silver"),
            "shfe_premium": safe_list("SHFE_Premium"),
            "gex": safe_list("GEX"),
            "dix": safe_list("DIX"),
            "reverse_repo_bn": safe_list("Reverse_Repo_BN"),
            "fed_balance_sheet_bn": safe_list("Fed_Balance_Sheet_BN"),
            "retail_silver_cheapest": safe_list("Retail_Silver_Cheapest"),
            "retail_silver_avg": safe_list("Retail_Silver_Avg"),
            "silver_oi": safe_list("Silver_OI"),
            "paper_physical_ratio": safe_list("Paper_Physical_Ratio")
        }
        
        return jsonify({"status": "success", "data": payload})
    
    except Exception as e:
        app.logger.error("Macro ledger failed: %s", e, exc_info=e)
        return json_error("Macro ledger failed (see the server log).", 500)

def _ledger_gamma_inputs(ticker_symbol):
    """(spot, zero_gamma, reason) from the pipeline's latest stored GEX row for the ticker. A value that is not
    stored is None, and `reason` says why the zero-gamma level is missing."""
    df = data_cache.get_csv(INSTITUTIONAL_LEDGER_CSV)
    if df.empty or 'Ticker' not in df:
        return None, None, "no stored GEX ledger yet: run the pipeline"
    rows = df[df['Ticker'].astype(str).str.upper() == ticker_symbol].tail(1)
    if rows.empty:
        return None, None, f"no stored GEX row for {ticker_symbol} (the pipeline records SPY and SLV)"
    row = rows.iloc[0]
    spot, zero_gamma = _num(row.get('Spot_Price')), _num(row.get('GEX_Zero_Gamma'))
    if spot is None or spot <= 0:
        return None, None, f"the latest stored GEX row for {ticker_symbol} has no spot price"
    if zero_gamma is None:
        return spot, None, f"the latest stored GEX row for {ticker_symbol} has no zero-gamma level"
    return spot, zero_gamma, None


def _atm_iv(calls, spot):
    """Implied volatility of the call nearest the spot price, or None."""
    if calls is None or calls.empty:
        return None
    iv = _num(calls.loc[(calls['strike'] - spot).abs().idxmin(), 'impliedVolatility'])
    return iv if iv is not None and iv > 0 else None


@app.route('/api/time_arbitrage')
@guarded("Time arbitrage", 502)
def get_time_arbitrage():
    ticker_symbol = ticker_arg()
    tk = yf.Ticker(ticker_symbol)
    # Anything that cannot be computed is None here, with the reason in `missing` (no stand-in numbers).
    missing = {}

    # 1. Oscillator: live VIX plus the latest GEX/DIX of the macro ledger. A component without a value is left
    # out of the composite and reported.
    current_vix = _live_spot(yf.Ticker("^VIX"))
    df_macro = data_cache.get_csv(LEDGER_CSV)
    latest_macro = df_macro.iloc[-1] if not df_macro.empty else None
    oscillator = quant.calculate_z_score_oscillator({
        'vix': current_vix,
        'gex': latest_macro.get('GEX') if latest_macro is not None else None,
        'dix': latest_macro.get('DIX') if latest_macro is not None else None,
    })
    z_score = oscillator['score']
    if z_score is None:
        why = "; ".join(f"{name}: {reason}" for name, reason in oscillator['components_missing'].items())
        missing['z_score'] = f"no oscillator component could be computed ({why})"

    # 2. Gamma state: the pipeline's stored spot and zero-gamma level for the ticker (a consistent pair)
    ledger_spot, zero_gamma, gamma_reason = _ledger_gamma_inputs(ticker_symbol)
    gamma_state = None
    if ledger_spot is not None and zero_gamma is not None:
        gamma_state = quant.get_gamma_state(ledger_spot, zero_gamma)
    else:
        missing['gamma_state'] = gamma_reason

    # Spot for the option maths: the live price, else the stored one
    spot_price = _live_spot(tk)
    if spot_price is None:
        spot_price = ledger_spot
    if spot_price is None:
        raise ApiError("Could not fetch the spot price.", 502)

    # 2b. Fetch Chain for Analytics & Filter 0DTE
    expirations = tk.options
    if not expirations:
        raise ApiError("No options available for this ticker.", 404)

    # Filter out 0DTE
    exp = expirations[0]
    for potential_exp in expirations:
        days = (datetime.strptime(potential_exp, '%Y-%m-%d') - datetime.now()).days
        if days > 0:
            exp = potential_exp
            break

    chain = tk.option_chain(exp)

    # ATM IV and Realized Volatility for the IV/HV Spread
    atm_iv = _atm_iv(chain.calls, spot_price)
    if atm_iv is None:
        missing['atm_implied_volatility'] = "no usable implied volatility on the call nearest the spot price"
    realized_vol = quant.calculate_realized_volatility(ticker_symbol)
    if realized_vol is None:
        missing['realized_volatility_20d'] = f"not enough price history for {ticker_symbol}"
    iv_hv_spread = {
        'realized_volatility_20d': realized_vol,
        'atm_implied_volatility': atm_iv
    }

    # Term Structure Caching
    term_structure = data_cache.get(f"{ticker_symbol}_term_structure")
    if not term_structure:
        term_structure = []
        targets = [7, 30, 90, 180]
        for target in targets:
            try:
                closest_exp = min(expirations, key=lambda x: abs((datetime.strptime(x, '%Y-%m-%d') - datetime.now()).days - target))
                chain_t = tk.option_chain(closest_exp)
                t_iv = _atm_iv(chain_t.calls, spot_price)
                days_t = max(1, (datetime.strptime(closest_exp, '%Y-%m-%d') - datetime.now()).days)
                if t_iv is not None:
                    term_structure.append({'days': days_t, 'iv': t_iv})
            except Exception as exc:
                app.logger.warning("term structure point %s %s failed: %s", ticker_symbol, target, exc)
        if term_structure:
            data_cache.set(f"{ticker_symbol}_term_structure", term_structure)
    if not term_structure:
        missing['term_structure'] = "no expiration returned a usable at-the-money implied volatility"

    # 3. IV Bleed & Historical Baseline: the 15 call strikes nearest the spot price, in strike order, against the
    # 20-day trailing realized volatility
    hist_iv_baseline = realized_vol
    calls = chain.calls
    near_strikes = calls.assign(_dist=(calls['strike'] - spot_price).abs()).nsmallest(15, '_dist').sort_values('strike')

    days_to_exp = (datetime.strptime(exp, '%Y-%m-%d') - datetime.now()).days
    if days_to_exp <= 0: days_to_exp = 1

    r_rate = quant.get_risk_free_rate()
    iv_bleed = []
    strikes_with_iv = []
    aggregate_vanna = 0.0
    aggregate_charm = 0.0
    vanna_profile = []

    for _, row in near_strikes.iterrows():
        live_iv = _num(row['impliedVolatility'])
        if live_iv is not None and live_iv <= 0:
            live_iv = None
        strike = float(row['strike'])

        iv_bleed.append({
            'strike': strike,
            'live_iv': live_iv,
            'hist_iv': hist_iv_baseline,
            'bleed': (live_iv - hist_iv_baseline) / hist_iv_baseline
                     if live_iv is not None and hist_iv_baseline else None
        })

        if live_iv is None:
            continue
        strikes_with_iv.append({'strike': strike, 'iv': live_iv})

        # Second-order Greeks, weighted by open interest (a contract with unknown open interest is left out)
        oi = _num(row['openInterest'])
        if r_rate is not None and oi is not None:
            vanna = quant.calculate_vanna(spot_price, strike, days_to_exp, r_rate, live_iv, is_call=True)
            charm = quant.calculate_charm(spot_price, strike, days_to_exp, r_rate, live_iv, is_call=True)
            aggregate_vanna += vanna * oi
            aggregate_charm += charm * oi
            vanna_profile.append({'strike': strike, 'vanna': float(vanna * oi), 'charm': float(charm * oi)})
    if hist_iv_baseline is None:
        missing['iv_bleed'] = "bleed needs the 20-day realized volatility"

    # 4. Probabilities (Skew-Adjusted & Time-Accurate): the 5 strikes with an implied volatility nearest the spot
    nearest = sorted(strikes_with_iv, key=lambda item: abs(item['strike'] - spot_price))[:5]
    nearest.sort(key=lambda item: item['strike'])
    prob_matrix = quant.calculate_strike_probabilities(spot_price, nearest, days_list=[3, 5, 7], r=r_rate)
    if r_rate is None:
        missing['probabilities'] = missing['dealer_trapdoor'] = "risk-free rate unavailable (Yahoo ^IRX)"
    elif not prob_matrix:
        missing['probabilities'] = "no strike near the spot price has an implied volatility"
    if r_rate is not None and not vanna_profile:
        missing['dealer_trapdoor'] = "no strike near the spot price has both implied volatility and open interest"

    dealer_trapdoor = {
        "vanna_exposure": float(aggregate_vanna) if vanna_profile else None,
        "charm_exposure": float(aggregate_charm) if vanna_profile else None,
        "vanna_profile": vanna_profile
    }

    return jsonify(_json_safe({
        "status": "success",
        "data": {
            "z_score": z_score,
            "z_components": {"used": oscillator['components_used'], "missing": oscillator['components_missing']},
            "gamma_state": gamma_state,
            "dealer_trapdoor": dealer_trapdoor,
            "iv_bleed": iv_bleed,
            "probabilities": prob_matrix,
            "term_structure": term_structure,
            "iv_hv_spread": iv_hv_spread,
            "missing": missing
        }
    }))


# ==========================================
# OPTION EXPLORER & ANALYTICS API
# ==========================================

@app.route('/api/option_chain', methods=['GET'])
@guarded("Option chain", 502)
def get_option_chain():
    """Returns all expirations and strikes for a ticker."""
    ticker_symbol = ticker_arg()
    expiration = request.args.get('expiration') or None
    tk = yf.Ticker(ticker_symbol)
    spot = _live_spot(tk)
    if spot is None:
        raise ApiError("Could not fetch the spot price.", 502)

    expirations = list(tk.options)
    if not expirations:
        raise ApiError("No options found for this ticker.", 404)

    if expiration and expiration in expirations:
        selected_exp = expiration
    else:
        # Default: first non-0DTE
        selected_exp = expirations[0]
        for e in expirations:
            if (datetime.strptime(e, '%Y-%m-%d') - datetime.now()).days > 0:
                selected_exp = e
                break

    chain = tk.option_chain(selected_exp)
    days_to_exp = max(1, (datetime.strptime(selected_exp, '%Y-%m-%d') - datetime.now()).days)

    def format_contracts(df, option_type):
        rows = []
        for _, row in df.iterrows():
            iv = row.get('impliedVolatility', 0)
            last = row.get('lastPrice', 0)
            bid = row.get('bid', 0)
            ask = row.get('ask', 0)
            oi = row.get('openInterest', 0)
            volume = row.get('volume', 0)
            rows.append({
                'strike': float(row['strike']),
                'type': option_type,
                'last': float(last) if pd.notna(last) else 0,
                'bid': float(bid) if pd.notna(bid) else 0,
                'ask': float(ask) if pd.notna(ask) else 0,
                'iv': float(iv) if pd.notna(iv) else 0,
                'oi': int(oi) if pd.notna(oi) else 0,
                'volume': int(volume) if pd.notna(volume) else 0,
            })
        return rows

    calls = format_contracts(chain.calls, 'call')
    puts = format_contracts(chain.puts, 'put')

    return jsonify({
        "status": "success",
        "data": {
            "ticker": ticker_symbol,
            "spot": spot,
            "expirations": expirations[:24],  # limit to next ~2 years
            "selected_expiration": selected_exp,
            "days_to_exp": days_to_exp,
            "calls": calls,
            "puts": puts,
        }
    })


@app.route('/api/option_calc', methods=['GET'])
@guarded("Option calculation", 502)
def calculate_option():
    """Runs full Black-Scholes analytics on a specific contract. It needs the contract's implied volatility from
    the live chain (422 when there is none) and the risk-free rate (503 when Yahoo has none): nothing is assumed."""
    ticker_symbol = ticker_arg()
    strike = float_arg('strike', 0.0)
    if strike <= 0:
        raise ApiError("strike must be a positive number", 400)
    expiration = request.args.get('expiration', '')
    try:
        expiration_date = datetime.strptime(expiration, '%Y-%m-%d')
    except ValueError:
        raise ApiError("expiration must be a date like 2026-12-18", 400)
    option_type = request.args.get('type', 'call').lower()
    if option_type not in ('call', 'put'):
        raise ApiError("type must be call or put", 400)
    market_price = float_arg('market_price', 0.0, minimum=0)

    tk = yf.Ticker(ticker_symbol)
    spot = _live_spot(tk)
    if spot is None:
        raise ApiError("Could not fetch the spot price.", 502)

    days = max(1, (expiration_date - datetime.now()).days)
    r = quant.get_risk_free_rate()
    if r is None:
        raise ApiError("risk-free rate unavailable (Yahoo ^IRX)", 503)

    # Pull IV from the live chain for accuracy
    try:
        chain = tk.option_chain(expiration)
    except Exception as exc:
        app.logger.warning("option chain %s %s failed: %s", ticker_symbol, expiration, exc)
        raise ApiError("option chain unavailable for this expiration", 502)
    df = chain.calls if option_type == 'call' else chain.puts
    match = df[df['strike'] == strike]
    if match.empty:
        raise ApiError("contract not found in the option chain, so it has no implied volatility", 422)
    iv = _num(match.iloc[0]['impliedVolatility'])
    if iv is None or iv <= 0:
        raise ApiError("the option chain has no implied volatility for this contract", 422)
    last_price = _num(match.iloc[0]['lastPrice'])
    if market_price == 0 and last_price is not None:
        market_price = last_price

    analytics = quant.calculate_option_analytics(
        spot=spot, strike=strike, days=days,
        r=r, vol=iv, option_type=option_type,
        market_price=market_price, ticker=ticker_symbol
    )
    analytics['spot'] = round(spot, 2)
    analytics['strike'] = strike
    analytics['expiration'] = expiration
    analytics['days_to_exp'] = days
    analytics['option_type'] = option_type.upper()

    return jsonify({"status": "success", "data": _json_safe(analytics)})

# ==========================================
# --- TRACKED ENGINE POSITIONS (v2_trade_signals) ---
# ==========================================


def _cached_underlying(sym):
    key = f"pos_quote:{sym}"
    hit = data_cache.get(key)
    if hit is not None:
        return hit
    try:
        closes = yf.Ticker(sym).history(period="5d", interval="1m")["Close"].dropna()
        if closes.empty:
            closes = yf.Ticker(sym).history(period="7d")["Close"].dropna()
        px = float(closes.iloc[-1]) if not closes.empty else None
    except Exception:
        px = None
    data_cache.set(key, px)
    return px


def _cached_chain(sym, exp):
    key = f"pos_chain:{sym}:{exp}"
    hit = data_cache.get(key)
    if hit is not None:
        return hit
    try:
        ch = yf.Ticker(sym).option_chain(exp)
        val = {"calls": ch.calls, "puts": ch.puts}
    except Exception:
        val = {}
    data_cache.set(key, val)
    return val


def _cached_daily_closes(sym):
    key = f"pos_daily:{sym}"
    hit = data_cache.get(key)
    if hit is not None:
        return hit
    try:
        s = yf.Ticker(sym).history(period="2y")["Close"].dropna()
        s.index = s.index.tz_localize(None).normalize()
    except Exception:
        s = pd.Series(dtype=float)
    data_cache.set(key, s)
    return s


def _uninitialised(exc):
    """True when a sqlite error means the database or one of its tables has not been created yet (no run has
    happened). Other errors (a locked or damaged file) are not that."""
    text = str(exc).lower()
    return "no such table" in text or "no such view" in text or "unable to open database" in text


def _not_initialised():
    return json_error("database not initialised: run the pipeline once", 503)


def _open_readonly():
    """A read-only connection to the pipeline database, or None when the file does not exist yet."""
    try:
        return _v2lake.connect_readonly(_V2_DB_PATH)
    except sqlite3.OperationalError as exc:
        if _uninitialised(exc):
            return None
        raise


@app.route('/api/positions', methods=['GET'])
@guarded("Positions")
def api_positions():
    conn = _open_readonly()
    if conn is None:
        return _not_initialised()
    try:
        rows = _v2pos.list_live(conn, _cached_underlying, _cached_chain, _cached_daily_closes)
    except sqlite3.OperationalError as exc:
        if _uninitialised(exc):
            return _not_initialised()
        raise
    finally:
        conn.close()
    return jsonify({"status": "success", "data": rows, "as_of": datetime.now().isoformat(timespec="seconds")})


def _writable_connection():
    """Read-write connection for star/stop. It never creates the database: no file means no run has happened."""
    if not os.path.exists(_V2_DB_PATH):
        return None
    return _v2lake.connect(_V2_DB_PATH)


@app.route('/api/brokerage', methods=['GET'])
@guarded("Brokerage positions", 502)
def api_brokerage():
    """Live SnapTrade holdings (stocks, option legs grouped into spreads), cached 60 s. status ok / not_configured / error."""
    from core import brokerage
    return jsonify(_json_safe(brokerage.positions(force=request.args.get('refresh') == '1')))


@app.route('/api/positions/<int:signal_id>/star', methods=['POST'])
@guarded("Star")
def api_position_star(signal_id):
    conn = _writable_connection()
    if conn is None:
        return _not_initialised()
    try:
        starred = _v2pos.toggle_star(conn, signal_id)
    except sqlite3.OperationalError as exc:
        if _uninitialised(exc):
            return _not_initialised()
        raise
    finally:
        conn.close()
    if starred is None:
        return json_error("not found", 404)
    return jsonify({"status": "success", "starred": starred})


@app.route('/api/positions/<int:signal_id>', methods=['DELETE'])
@guarded("Stop tracking")
def api_position_delete(signal_id):
    conn = _writable_connection()
    if conn is None:
        return _not_initialised()
    try:
        ok = _v2pos.stop_tracking(conn, signal_id)
    except sqlite3.OperationalError as exc:
        if _uninitialised(exc):
            return _not_initialised()
        raise
    finally:
        conn.close()
    return jsonify({"status": "success"}) if ok else json_error("not found", 404)



# ==========================================
# --- FORECAST LAB (reads the latest committed v2 snapshot) ---
# ==========================================
from core import edges as _v2edges, watch as _v2watch


def _scanner_payload(refresh=False):
    symbols = _v2watch.watchlist()
    if refresh:                                        # the card's REFRESH button: back to Yahoo for everything it shows
        _v2watch.refresh()
        for key in [k for k in data_cache.cache if k.startswith(("pos_quote:", "pos_chain:"))]:
            data_cache.cache.pop(key, None)
    conn, positions = _writable_connection(), []       # no database yet: no paper positions, the scan still works
    if conn is not None:
        try:
            _v2lake.migrate(conn)
            positions = _v2watch.list_positions(conn, _cached_chain)
        finally:
            conn.close()
    rows, failed = [], []
    for sym in symbols:
        try:
            closes, live = _v2watch.fetch_closes(sym), _cached_underlying(sym)
            held = sym == "SPY" and any(not p["closed_at"] for p in positions)
            dip = _v2watch.dip_signal(closes, live, _v2watch.in_entry_window(), held, sym, _v2watch.fetch_blackout(sym))
            if not dip:
                raise ValueError("no quote")
            rows.append(dip)
        except Exception:
            failed.append(sym)                         # no history or quote right now: listed, so it can still be removed
    return {"status": "success", "scanner": rows, "watch_positions": positions, "symbols": symbols, "failed": failed,
            "max_symbols": _v2watch.MAX_SYMBOLS + 1, "as_of": datetime.now().isoformat(timespec="seconds")}


@app.route('/api/scanner', methods=['GET', 'POST'])
@guarded("Day scanner", 502)
def api_scanner():
    if request.method == 'POST':
        err = _v2watch.add_symbol((request.get_json(silent=True) or {}).get("symbol"))
        if err:
            return jsonify({"status": "error", "message": err}), 400
    return jsonify(_scanner_payload(refresh=request.args.get("refresh") == "1"))


@app.route('/api/edges', methods=['GET', 'POST'])
@guarded("Edge lab", 502)
def api_edges():
    """Edge lab: every tracked ticker, plus ?symbol=X analysed without saving. POST {symbol} tracks one."""
    query = request.args.get("symbol")
    if request.method == 'POST':
        query = (request.get_json(silent=True) or {}).get("symbol")
        err = _v2edges.add_tracked(query)
        if err:
            return jsonify({"status": "error", "message": err}), 400
    try:
        return jsonify(_v2edges.payload(query, fresh=request.args.get("refresh") == "1"))
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/edges/<symbol>', methods=['DELETE'])
@guarded("Edge lab", 502)
def api_edges_remove(symbol):
    _v2edges.remove_tracked(symbol)
    return jsonify(_v2edges.payload())


@app.route('/api/scanner/<symbol>', methods=['DELETE'])
@guarded("Day scanner", 502)
def api_scanner_remove(symbol):
    _v2watch.remove_symbol(symbol)
    return jsonify(_scanner_payload())


def _latest_forecast(conn):
    """(run, forecast dict) of the latest committed snapshot, or (None, None) when there is none yet."""
    row = conn.execute("SELECT run_id FROM v2_latest_snapshot").fetchone()
    if not row:
        return None, None
    key = f"fc_snapshot:{row['run_id']}"
    hit = data_cache.get(key)
    if hit is None:
        ctx = _v2lake.load_snapshot(conn, row["run_id"])
        hit = {"run": ctx["run"], "forecast": ctx.get("forecast"), "refining": ctx.get("refining"), "chaos": ctx.get("chaos"),
               "fed_ops": ctx.get("fed_ops")}
        data_cache.set(key, hit)
    return hit["run"], dict(hit["forecast"] or {}, _refining=hit.get("refining"), chaos=hit.get("chaos"), fed_ops=hit.get("fed_ops"))


@app.route('/api/forecast', methods=['GET'])
@guarded("Forecast")
def api_forecast():
    ticker = (request.args.get('ticker') or 'SPY').upper()
    if ticker not in ("SPY", "SLV"):
        return json_error("ticker must be SPY or SLV", 400)
    conn = _open_readonly()
    if conn is None:
        return _not_initialised()
    try:
        run, fc = _latest_forecast(conn)
        if not fc or fc.get("status") == "error":
            return jsonify({"status": "error", "message": (fc or {}).get("reason") or "no Forecast Lab data yet: run the pipeline",
                            "run": run}), 404
        score = _v2fc.scorecard(conn, _cached_daily_closes, ticker)
        ticket = _v2pos.run_ticket(conn, run["run_id"])
        quotes = {k: _cached_underlying(s) for k, s in (("SPY", "SPY"), ("SLV", "SLV"), ("SI_F", "SI=F"))}
        signals = _v2fc.signal_watch(_v2fc.live_overlay(fc, {k: v for k, v in quotes.items() if v}), ticket)
    except sqlite3.OperationalError as exc:
        if _uninitialised(exc):
            return _not_initialised()
        raise
    finally:
        conn.close()
    return jsonify({"status": "success", "ticker": ticker, "run": run, "horizons": fc.get("horizons"),
                    "data": fc.get(ticker), "consensus": (fc.get("consensus") or {}).get(ticker),
                    "positioning": fc.get("positioning"), "silver_fair_value": fc.get("silver_fair_value"),
                    "macro_regime": fc.get("macro_regime"), "source_errors": fc.get("source_errors"),
                    "refining": fc.get("_refining"), "chaos": fc.get("chaos"), "fed_ops": fc.get("fed_ops"),
                    "scorecard": score, "signals": signals})


@app.route('/api/shanghai_spread', methods=['GET'])
@guarded("Shanghai spread")
def api_shanghai_spread():
    """The latest snapshot's Shanghai silver section: the legacy fields as stored and the date-paired spread."""
    conn = _open_readonly()
    if conn is None:
        return _not_initialised()
    try:
        run_id, snap = _snapshot_sections(conn)
        if not run_id:
            return json_error("No committed run yet.", 404)
        sh = snap["shanghai"]
        data = {"run_at": snap["run_at"],
                "legacy": {k: sh.get(k) for k in ("status", "reason", "sge_date", "sge_session", "cny_per_kg", "usdcny",
                                                  "usd_per_oz", "comex_ref", "raw_spread", "premium_tax_adj")},
                "dated": sh.get("dated")}
    except sqlite3.OperationalError as exc:
        if _uninitialised(exc):
            return _not_initialised()
        raise
    finally:
        conn.close()
    return jsonify({"status": "success", "data": _json_safe(data)})


@app.route('/api/paper_physical', methods=['GET'])
@guarded("Paper physical")
def api_paper_physical():
    """The latest snapshot's paper_physical section as stored (ratio, tier, inputs, recorded-ratio history)."""
    conn = _open_readonly()
    if conn is None:
        return _not_initialised()
    try:
        run_id, snap = _snapshot_sections(conn)
        if not run_id:
            return json_error("No committed run yet.", 404)
    except sqlite3.OperationalError as exc:
        if _uninitialised(exc):
            return _not_initialised()
        raise
    finally:
        conn.close()
    return jsonify({"status": "success",
                    "data": _json_safe({k: snap[k] for k in ("run_at", "paper_physical")})})


@app.route('/api/chaos', methods=['GET'])
@guarded("Chaos")
def api_chaos():
    """The latest committed snapshot's Chaos / Peace block (Forecast Lab card 14)."""
    conn = _open_readonly()
    if conn is None:
        return _not_initialised()
    try:
        run, fc = _latest_forecast(conn)
    except sqlite3.OperationalError as exc:
        if _uninitialised(exc):
            return _not_initialised()
        raise
    finally:
        conn.close()
    if not run or not fc or not fc.get("chaos"):
        return json_error("no Chaos / Peace data yet: run the pipeline", 404)
    return jsonify({"status": "success", "run": run, "as_of": fc["chaos"].get("as_of"), "data": fc["chaos"]})


@app.route('/api/chaos/history', methods=['GET'])
@guarded("Chaos history")
def api_chaos_history():
    """The newest stored `chaos.score` observation per date for the current model version, oldest first."""
    since = request.args.get("from")
    if since:
        try:
            since = date.fromisoformat(since).isoformat()
        except ValueError:
            return json_error("from must be a date as YYYY-MM-DD", 400)
    conn = _open_readonly()
    if conn is None:
        return _not_initialised()
    key = f"chaos_history:{since}"
    data = data_cache.get(key)
    if data is None:
        try:
            rows = conn.execute("SELECT observed_at, value_num, dims_json FROM v2_observations WHERE metric_id = 'chaos.score' AND derivation = ? "
                                "AND value_num IS NOT NULL AND observed_at >= ? ORDER BY observed_at, obs_id",
                                (CHAOS_VERSION, since or "")).fetchall()
        except sqlite3.OperationalError as exc:
            if _uninitialised(exc):
                return _not_initialised()
            raise
        finally:
            conn.close()
        newest = {r[0][:10]: (r[1], json.loads(r[2]).get("used")) for r in rows}
        if not newest:
            return json_error("no Chaos / Peace history yet: run the pipeline", 404)
        days = sorted(newest)
        data = {"version": CHAOS_VERSION, "t": [(date.fromisoformat(d) - date(1970, 1, 1)).days for d in days],
                "score": [newest[d][0] for d in days], "used": [newest[d][1] for d in days], "first": days[0], "last": days[-1], "n": len(days)}
        data_cache.set(key, data)
    else:
        conn.close()
    return jsonify({"status": "success", "data": data})


FED_OPS_PARTS = ("repo", "buybacks", "auctions")
# repo: the latest reading per market and rate is /api/fed_ops/repo; the history carries the period levels only
FED_OPS_SUMMARY = {"repo": ("status", "as_of", "reason", "rrp_trend", "rates_status", "yearly", "monthly", "units", "note"),
                   "buybacks": ("this_year_vs_last_year_same_dates", "status", "as_of", "reason", "last_8", "scheduled", "largest_5", "monthly", "quarterly", "yearly", "t12m", "prior_t12m", "t12m_by_security_type", "units"),
                   "auctions": ("status", "as_of", "reason", "upcoming", "upcoming_status", "by_tenor", "monthly", "t12m", "prior_t12m", "tail", "units")}


_FED_OPS_FALLBACK = {}


def _fed_ops_section(conn):
    """(run, block): the latest snapshot's fed_ops block. Until a run has built one (or when the run's block failed), the same
    block summarised from the stored rows (no provider is called), every part marked stale with the date of its newest row,
    and rebuilt only when stored rows change. (run, None) when nothing is stored."""
    from core import fedops as _v2fo
    run, fc = _latest_forecast(conn)
    sec = (fc or {}).get("fed_ops")
    if sec and sec.get("status") != "error" and sec.get("version") == _v2fo.FEDOPS_VERSION:
        return run, sec
    # An older model version (a run before the deploy) is rebuilt from the same stored rows with the current code: that run
    # refreshed them, so its parts keep their status. No snapshot, or a failed block: every part is marked stale.
    outdated = bool(sec) and sec.get("status") != "error"
    metrics = (_v2fo.REPO, _v2fo.RATE, _v2fo.BUYBACK, _v2fo.AUCTION)
    stamp = conn.execute("SELECT MAX(obs_id) FROM v2_observations WHERE metric_id IN (?,?,?,?)", metrics).fetchone()[0]
    upcoming = conn.execute("SELECT MAX(payload_id) FROM v2_payloads WHERE kind = 'treasury_upcoming'").fetchone()[0]
    if stamp is None:
        return run, None
    today = datetime.now(ZoneInfo("America/New_York")).date()
    key = (today, stamp, upcoming, bool(sec), outdated)
    if key not in _FED_OPS_FALLBACK:
        if outdated:          # what that run could not refresh stays stale
            parts = {"nyfed:rp": sec.get("repo"), "nyfed:rates": (sec.get("repo") or {}).get("rates_status"), "treasury:buybacks": sec.get("buybacks"),
                     "treasury:auctions": sec.get("auctions"), "treasury:upcoming": (sec.get("auctions") or {}).get("upcoming_status")}
            errors = {k: p.get("reason") or "not refreshed by the latest run" for k, p in parts.items() if isinstance(p, dict) and p.get("status") in ("stale", "missing")}
            label = f"rebuilt from stored rows with {_v2fo.FEDOPS_VERSION} (the latest run's block is {sec.get('version')})"
        else:
            why = "the latest pipeline run could not build card 15" if sec else "no pipeline run has refreshed it yet"
            errors, label = {k: why for k in ("nyfed:rp", "nyfed:rates", "treasury:buybacks", "treasury:auctions", "treasury:upcoming")}, f"stored history ({why})"
        _FED_OPS_FALLBACK.clear()
        _FED_OPS_FALLBACK[key] = dict(_v2fo.build(conn, today, errors), source=label)
    return run, _FED_OPS_FALLBACK[key]


@app.route('/api/fed_ops', methods=['GET'])
@app.route('/api/fed_ops/repo', methods=['GET'])
@app.route('/api/fed_ops/buybacks', methods=['GET'])
@app.route('/api/fed_ops/auctions', methods=['GET'])
@guarded("Fed operations")
def api_fed_ops():
    """Card 15 from the latest committed snapshot: repo and reverse repo operations with the reference rates, Treasury
    buybacks, Treasury auctions (latest readings and summaries). /api/fed_ops/<part> narrows it to one part."""
    part = request.path.rsplit("/", 1)[-1]
    part = part if part in FED_OPS_PARTS else None
    conn = _open_readonly()
    if conn is None:
        return _not_initialised()
    try:
        run, sec = _fed_ops_section(conn)
    except sqlite3.OperationalError as exc:
        if _uninitialised(exc):
            return _not_initialised()
        raise
    finally:
        conn.close()
    if not sec:
        return json_error("no Fed / Treasury operations data yet: run the pipeline", 404)
    return jsonify({"status": "success", "run": run, "as_of": sec.get("as_of"), "data": sec[part] if part else sec})


GUIDE_SKIP = {"what", "whats", "the", "and", "does", "mean", "means", "explain", "how", "why", "are", "for", "this", "that", "card", "like", "new", "simple", "terms", "program", "tell", "about"}


@app.route('/api/guide', methods=['GET'])
@guarded("guide")
def api_guide():
    """The plain-English guide (docs/guide.md): the two sections that best match `term` (heading words first, then text);
    no term lists the headings."""
    with open(os.path.join(ROOT_DIR, "docs", "guide.md"), encoding="utf-8") as fh:
        parts = [p.partition("\n") for p in re.split(r"(?m)^#{2,3} ", fh.read())[1:]]
    sections = [(h.strip(), b.strip()) for h, _, b in parts if b.strip()]
    words = [w for w in re.findall(r"[a-z0-9]+", (request.args.get("term") or "").lower()) if len(w) > 2 and w not in GUIDE_SKIP]
    if not words:
        return jsonify({"status": "success", "headings": [h for h, _ in sections]})
    if "questions" not in words:            # its example figures are one day's readings; a model quoted them as live
        sections = [s for s in sections if not s[0].startswith("Ten questions")]
    score = lambda s: (sum(w in s[0].lower() for w in words), sum(w in s[1].lower() for w in words))
    hits = [s for s in sorted(sections, key=score, reverse=True) if any(score(s))][:2]
    if not hits:
        return jsonify({"status": "missing", "reason": f"the guide has no entry for {' '.join(words)}", "headings": [h for h, _ in sections]})
    return jsonify({"status": "success", "sections": [{"heading": h, "text": b} for h, b in hits]})


@app.route('/api/fed_ops/history/repo', methods=['GET'])
@app.route('/api/fed_ops/history/buybacks', methods=['GET'])
@app.route('/api/fed_ops/history/auctions', methods=['GET'])
@guarded("Fed operations history")
def api_fed_ops_history():
    """Stored daily repo / rate rows, buyback operations or auction results between `from` and `to` (the newest `limit`
    of them, oldest first), with the latest snapshot's precomputed summaries for that kind."""
    from core import fedops as _v2fo
    kind = request.path.rsplit("/", 1)[-1]
    try:
        start, end = (date.fromisoformat(request.args[k]).isoformat() if request.args.get(k) else None for k in ("from", "to"))
        limit = max(1, min(int(request.args.get("limit") or 60), 5000))
    except ValueError:
        return json_error("from and to must be dates as YYYY-MM-DD; limit a whole number", 400)
    tenor, sec_type = request.args.get("tenor") or "", (request.args.get("security_type") or "").lower()
    conn = _open_readonly()
    if conn is None:
        return _not_initialised()
    try:
        if kind == "repo":
            metric, rows = _v2fo.REPO, _v2fo.repo_daily(conn, start, end)
        elif kind == "buybacks":
            metric = _v2fo.BUYBACK
            rows = [{k: v for k, v in r.items() if k != "entity"} for r in _v2fo.read(conn, metric, start, end)
                    if sec_type in (r.get("security_type") or "").lower()]
        else:
            metric = _v2fo.AUCTION
            rows = [{"date": r["date"], "tenor": _v2fo.tenor(r), "cusip": r["entity"], **{k: v for k, v in r.items() if k not in ("date", "entity", "term", "type")}}
                    for r in _v2fo.read(conn, metric, start, end) if _v2fo.tenor_matches(tenor, _v2fo.tenor(r))]
        first = _v2fo.earliest(conn, metric)
        run, sec = _fed_ops_section(conn)
    except sqlite3.OperationalError as exc:
        if _uninitialised(exc):
            return _not_initialised()
        raise
    finally:
        conn.close()
    if first is None:
        return json_error("no stored history yet: run scripts/backfill_fed_ops.py or the pipeline", 404)
    summary = {k: (sec or {}).get(kind, {}).get(k) for k in FED_OPS_SUMMARY[kind]} if sec else {"status": "missing", "reason": "no stored history yet"}
    def overlaps(p):          # p is a year "2026", a month "2026-09" or a quarter "2026-Q3"
        y, rest = p[:4], p[5:]
        first, last = ((f"{y}-{3 * int(rest[1]) - 2:02d}", f"{y}-{3 * int(rest[1]):02d}") if rest.startswith("Q")
                       else (p[:7], p[:7]) if rest else (f"{y}-01", f"{y}-12"))
        return (not start or last >= start[:7]) and (not end or first <= end[:7])
    for key in ("monthly", "quarterly", "yearly"):
        if isinstance(summary.get(key), list):
            summary[key] = [r for r in summary[key] if overlaps(str(r.get("month") or r.get("period") or r.get("year")))]
    if kind == "auctions" and tenor:
        summary["by_tenor"] = {k: v for k, v in (summary.get("by_tenor") or {}).items() if _v2fo.tenor_matches(tenor, k)}
        for key in ("monthly", "t12m", "prior_t12m"):     # market-wide totals: a 9B reported them as the tenor's
            summary.pop(key, None)
    return jsonify({"status": "success", "kind": kind, "earliest_stored": first, "from": start, "to": end, "rows_in_range": len(rows),
                    "rows": rows[-limit:], "summary": summary, "run": run})


_EIA_HISTORY_CACHE = {}


@app.route('/api/eia_history', methods=['GET'])
def api_eia_history():
    """Full weekly history behind Forecast Lab card 11, built from the EIA files the pipeline already captured."""
    from core import refining as _v2rf, sources as _v2src
    keys = sorted({k for k, _ in _v2rf.INVENTORY_LINES} | set(_v2rf.DEMAND_FOR.values()))
    conn = _open_readonly()
    if conn is None:
        return _not_initialised()
    try:
        rows = {}
        for key in keys:
            row = conn.execute("""SELECT payload_id FROM v2_payloads WHERE kind = 'eia_weekly' AND request_json LIKE ?
                                  ORDER BY fetched_at DESC LIMIT 1""", (f'%"series": "{_v2rf.EIA_SERIES[key]}"%',)).fetchone()
            if row:
                rows[key] = row["payload_id"]
        stamp = tuple(sorted(rows.items()))
        if _EIA_HISTORY_CACHE.get("stamp") != stamp:
            series = {}
            for key, pid in rows.items():
                _, content = _v2lake.load_payload(conn, pid)
                if content:
                    series[key] = _v2src._eia_parse(content)
            _EIA_HISTORY_CACHE.update(stamp=stamp, data=_v2rf.inventory_history(series))
        hist = _EIA_HISTORY_CACHE["data"]
    except Exception as e:
        if isinstance(e, sqlite3.OperationalError) and _uninitialised(e):
            return _not_initialised()
        app.logger.error("EIA history failed: %s", e, exc_info=e)
        return json_error("EIA history failed (see the server log).", 500)
    finally:
        conn.close()
    if hist.get("status") != "fresh":
        return json_error(hist.get("reason") or "no EIA data yet: run the pipeline", 404)
    return jsonify(dict(hist, status="success"))


# ---------------------------------------------------------------- ask-in-words assistant (read-only)
import assistant as _assistant

_assistant.register(app)


class QuietHandler(WSGIRequestHandler):
    """No Werkzeug or Python version in the Server header."""
    def version_string(self):
        return "market-terminal"


if __name__ == "__main__":
    # 127.0.0.1:8080 unless OPTIONS_WHALE_HOST / OPTIONS_WHALE_PORT are set in .env (per-machine, never committed)
    app.run(host=config.OPTIONS_WHALE_HOST, port=config.OPTIONS_WHALE_PORT, request_handler=QuietHandler)
