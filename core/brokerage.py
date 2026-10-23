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


def _leg(p):
    """An option position from /positions/all, or None when it lacks what grouping needs."""
    try:
        return _leg_raw(p)
    except (AttributeError, ValueError, TypeError):
        return None


def _leg_raw(p):
    i = p.get("instrument") or {}
    if i.get("kind") != "option":
        return None
    under = (i.get("underlying") or {}).get("symbol")
    strike, units = _num(i.get("strike_price")), _num(p.get("units"))
    if not (under and strike is not None and i.get("expiration_date") and units):
        return None
    return {"underlying": under, "type": str(i.get("option_type", "")).upper(), "strike": strike,
            "expiry": str(i["expiration_date"])[:10], "units": units, "multiplier": _num(i.get("multiplier")) or 100,
            "price": _num(p.get("price")), "avg_price": _num(p.get("cost_basis")), "ticker": i.get("symbol")}


STOCK_KINDS = {"stock", "etf", "adr", "cef", "mutualfund", "crypto", "bond", "other"}


def _stock(p):
    try:
        return _stock_raw(p)
    except (AttributeError, ValueError, TypeError):
        return None


def _stock_raw(p):
    i = p.get("instrument") or {}
    units, price, cost = _num(p.get("units")), _num(p.get("price")), _num(p.get("cost_basis"))
    if i.get("kind") not in STOCK_KINDS or not i.get("symbol") or not units:
        return None
    return {"symbol": i["symbol"], "units": units, "price": price, "avg_price": cost,
            "value": units * price if price is not None else None,
            "open_pnl": (price - cost) * units if price is not None and cost is not None else None}


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
        marks = [l["units"] * l["price"] * l["multiplier"] for l in ls if l["price"] is not None]
        pnls = [(l["price"] - l["avg_price"]) * l["units"] * l["multiplier"] for l in ls if l["price"] is not None and l["avg_price"] is not None]
        out.append({"account": ls[0].get("account_name", acct), "underlying": under, "expiry": expiry, "kind": kind, "legs": ls,
                    "width": max(strikes) - min(strikes) if kind.endswith("SPREAD") else None,
                    # mark value is signed: shorts subtract. None when any leg has no price (missing is not zero).
                    "net_mark": sum(marks) if len(marks) == len(ls) else None,
                    "open_pnl": sum(pnls) if len(pnls) == len(ls) else None})
    return out


def fetch(creds):
    stocks, legs, accounts, skipped = [], [], [], 0
    for acct in _call("GET", "/api/v1/accounts", creds):
        aid = acct["id"]
        accounts.append({"id": aid, "name": acct.get("name"), "institution": acct.get("institution_name")})
        got = _call("GET", f"/api/v1/accounts/{aid}/positions/all", creds)   # stocks, ETFs and options in one list (the old /positions and /options answer 410)
        for p in got.get("results", []) if isinstance(got, dict) else got:
            if (stock := _stock(p)):
                stocks.append(stock)
            elif (leg := _leg(p)):
                legs.append({**leg, "account": aid, "account_name": acct.get("name") or aid})
            else:
                skipped += 1   # unlisted kind (future, warrant...) or an unparseable row: counted, never silently zero
    return {"accounts": accounts, "stocks": stocks, "options": group_options(legs), "skipped": skipped}


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
