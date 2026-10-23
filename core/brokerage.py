"""Brokerage positions through SnapTrade (read-only): stock holdings and option legs grouped into spreads.

Live, not stored: the terminal route calls this on refresh and `positions()` caches the answer for CACHE_SECONDS.
Without the four SNAPTRADE_* settings it answers {"status": "not_configured", ...} with the setup steps the card shows.
Personal SnapTrade keys identify the account themselves, so only the client id and consumer key are needed.
"""
import base64
import hashlib
import hmac
import json
import threading
import time
from collections import defaultdict
from urllib.parse import quote, urlencode

import requests

import config

BASE = "https://api.snaptrade.com"
CACHE_SECONDS = 60
ERROR_CACHE_SECONDS = 30
SETTINGS = ("SNAPTRADE_CLIENT_ID", "SNAPTRADE_CONSUMER_KEY")
SETUP = [
    "Sign in at dashboard.snaptrade.com (a Personal account) and connect your broker there.",
    "Copy the Client ID and Consumer Key from the dashboard into Bitwarden as SNAPTRADE_CLIENT_ID and SNAPTRADE_CONSUMER_KEY.",
    "Run scripts/sync_secrets.sh pull (Bitwarden -> Keychain), then restart the terminal.",
]

_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()


def _creds():
    return {k: config.optional_env(k, default="") for k in SETTINGS}


def sign(consumer_key, path, query, body=None):
    """SnapTrade request signature: HMAC-SHA256 over the compact, key-sorted JSON of content/path/query."""
    payload = json.dumps({"content": body, "path": path, "query": query}, separators=(",", ":"), sort_keys=True)
    digest = hmac.new(quote(consumer_key, safe="~()*!.'").encode(), payload.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _call(method, path, creds, body=None):
    params = {"clientId": creds["SNAPTRADE_CLIENT_ID"], "timestamp": str(int(time.time()))}
    query = urlencode(params)
    resp = requests.request(method, f"{BASE}{path}", params=params, json=body, timeout=15,
                            headers={"Signature": sign(creds["SNAPTRADE_CONSUMER_KEY"], path, query, body)})
    resp.raise_for_status()
    return resp.json()


def _num(x):
    try:
        return float(x)
    except (ValueError, TypeError):
        return None


def _leg(h):
    try:
        return _leg_raw(h)
    except (ValueError, TypeError):
        return None


def _leg_raw(h):
    o = (h.get("symbol") or {}).get("option_symbol") or {}
    under = (o.get("underlying_symbol") or {}).get("symbol")
    if not (under and o.get("strike_price") is not None and o.get("expiration_date")):
        return None
    return {"underlying": under, "type": str(o.get("option_type", "")).upper(), "strike": float(o["strike_price"]),
            "expiry": str(o["expiration_date"])[:10], "units": float(h.get("units") or 0),
            "price": _num(h.get("price")), "avg_price": _num(h.get("average_purchase_price")),
            "ticker": o.get("ticker")}


def _stock(p):
    try:
        return _stock_raw(p)
    except (ValueError, TypeError):
        return None


def _stock_raw(p):
    sym = ((p.get("symbol") or {}).get("symbol") or {}).get("symbol")
    units, price = _num(p.get("units")), _num(p.get("price"))
    if not sym or not units:
        return None
    return {"symbol": sym, "units": units, "price": price, "avg_price": _num(p.get("average_purchase_price")),
            "value": units * price if price is not None else None, "open_pnl": _num(p.get("open_pnl"))}


def classify(legs):
    """Name a group of option legs sharing underlying and expiry. Unrecognised shapes stay "MULTI-LEG"."""
    legs = sorted(legs, key=lambda l: l["strike"])
    side = lambda l: "LONG" if l["units"] > 0 else "SHORT"
    if len(legs) == 1:
        return f"{side(legs[0])} {legs[0]['type']}"
    if len(legs) == 2:
        a, b = legs
        if a["type"] == b["type"] and (a["units"] > 0) != (b["units"] > 0):
            long_low = a["units"] > 0
            debit = long_low if a["type"] == "CALL" else not long_low
            return f"{a['type']} {'DEBIT' if debit else 'CREDIT'} SPREAD"
        if a["type"] != b["type"] and (a["units"] > 0) == (b["units"] > 0):
            return f"{side(a)} {'STRADDLE' if a['strike'] == b['strike'] else 'STRANGLE'}"
    return "MULTI-LEG"


def group_options(legs):
    """Group by account, underlying and expiry. ponytail: calendars/diagonals (several expiries) and two spreads on one
    underlying and expiry stay split/merged; pair them by hand if that matters."""
    groups = defaultdict(list)
    for leg in legs:
        groups[(leg.get("account"), leg["underlying"], leg["expiry"])].append(leg)
    out = []
    for (acct, under, expiry), ls in sorted(groups.items(), key=lambda kv: (kv[0][2], kv[0][1], str(kv[0][0]))):
        kind = classify(ls)
        strikes = [l["strike"] for l in ls]
        marks = [l["units"] * l["price"] * 100 for l in ls if l["price"] is not None]
        out.append({"account": acct, "underlying": under, "expiry": expiry, "kind": kind, "legs": ls,
                    "width": max(strikes) - min(strikes) if kind.endswith("SPREAD") else None,
                    # mark value is signed: shorts subtract. None when any leg has no price (missing is not zero).
                    "net_mark": sum(marks) if len(marks) == len(ls) else None})
    return out


def fetch(creds):
    stocks, legs, accounts, seen = [], [], [], 0
    for acct in _call("GET", "/api/v1/accounts", creds):
        aid = acct["id"]
        accounts.append({"id": aid, "name": acct.get("name"), "institution": acct.get("institution_name")})
        stocks += [s for s in map(_stock, _call("GET", f"/api/v1/accounts/{aid}/positions", creds)) if s]
        held = _call("GET", f"/api/v1/accounts/{aid}/options", creds)
        held = held.get("holdings", []) if isinstance(held, dict) else held
        for l in map(_leg, held):
            if l:
                legs.append({**l, "account": acct.get("name") or aid})
    return {"accounts": accounts, "stocks": stocks, "options": group_options(legs)}


def positions(force=False):
    """The card's payload: status "ok", "not_configured" (with setup steps) or "error" (with the reason)."""
    creds = _creds()
    missing = [k for k in SETTINGS if not creds[k]]
    if missing:
        return {"status": "not_configured", "missing": missing, "setup": SETUP,
                "reason": "SnapTrade is not set up: " + ", ".join(missing) + " empty."}
    with _lock:
        if not force and _cache["data"] and time.time() - _cache["at"] <= CACHE_SECONDS:
            return _cache["data"]
        try:
            data = {"status": "ok", "as_of": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **fetch(creds)}
        except requests.HTTPError as exc:
            code = exc.response.status_code
            hint = " Check the client id and consumer key." if code in (401, 403) else ""
            data = {"status": "error", "reason": f"SnapTrade answered HTTP {code}.{hint}", "setup": SETUP}
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            data = {"status": "error", "reason": f"SnapTrade call failed: {type(exc).__name__}", "setup": SETUP}
        _cache.update(at=time.time() - (0 if data["status"] == "ok" else CACHE_SECONDS - ERROR_CACHE_SECONDS), data=data)   # errors are held 30 s, so a persistent 401/429 is not retried on every poll
        return data
