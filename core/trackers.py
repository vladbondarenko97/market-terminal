"""T · Trackers: the operator's own conditions with ntfy alerts.

A tracker is a name, one to four conditions that must all hold (each `expression comparator threshold`, or `changes`), a priority
(ntfy 1 to 5), a cooldown, active hours (24/7 or the NYSE session), a note and an on/off switch. An expression is + - * / ( ), numbers
and operands, read with `ast` (never `eval`, same check as core/formulas.py). An operand is:
  - a symbol in capitals, its live last price: `SLV`, `^VIX`, `BTC-USD`, `SI=F` (spaces around a binary minus, like the CHART formulas);
  - a metric of the latest live snapshot: a short name from METRICS below (`hy_oas`, `chaos`, `regime`, `btc_verdict`) or any
    core/catalog.py metric id (`macro.vmri.score`, `options.SPY.net_gex`).
A reading that cannot be had is an error for that evaluation, never a zero: the tracker neither fires nor re-arms.

The trackers live in one JSON file (config.TRACKERS_FILE, else the data folder). What the job learns (last value, armed or not, last fire)
goes to a second file beside it, `trackers_state.json`, which only the job writes, so a save from the terminal cannot clobber it; every
fire (and test) is appended to `trackers_fires.jsonl`. Pure functions take their inputs; the only clock and network are the arguments.
"""
import ast
import json
import logging
import math
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import config
from core import catalog, formulas

log = logging.getLogger(__name__)
PRIORITIES = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}
OPS = ("<", "<=", ">", ">=", "==", "changes")
HOURS = ("24/7", "session")
MAX_TRACKERS, MAX_CONDITIONS, MAX_COOLDOWN_MIN = 60, 4, 7 * 24 * 60
BAR_SCALE_PCT = 25.0         # distance bar: empty at 25% or more away from the trigger, full when it is met (display only)
TAGS = {5: "rotating_light", 4: "warning", 3: "bell", 2: "mag", 1: "zzz"}


# ============================================================ metrics of the latest live snapshot
def _g(ctx, *path):
    for p in path:
        if not isinstance(ctx, dict) or p not in ctx:
            return None
        ctx = ctx[p]
    return ctx


def _lever(ctx, key):
    return next((r for r in (_g(ctx, "chaos", "levers", "rows") or []) if r.get("key") == key and r.get("status") == "fresh"), None)


def _repo(ctx, field):
    rows = _g(ctx, "fed_ops", "repo", "last_60d") or []
    return next((r[field] for r in reversed(rows) if r.get(field) is not None), None)


def _sofr_effr(ctx):
    rows = _g(ctx, "fed_ops", "repo", "last_60d") or []
    r = next((r for r in reversed(rows) if r.get("sofr") is not None and r.get("effr") is not None), None)
    return None if r is None else round(r["sofr"] - r["effr"], 3)


# short name -> (what it is, getter(ctx) -> value or None)
METRICS = {
    "vix": ("VIX at the last run", lambda c: _g(c, "prices", "vix", "value")),
    "vix3m": ("VIX3M at the last run", lambda c: _g(c, "prices", "vix3m", "value")),
    "vvix": ("VVIX (Chaos component)", lambda c: _g(c, "chaos", "components", "vvix", "value")),
    "chaos": ("Chaos / Peace score", lambda c: _g(c, "chaos", "score")),
    "chaos_regime": ("Chaos / Peace regime (text)", lambda c: _g(c, "chaos", "regime")),
    "hy_oas": ("High-yield OAS, percent", lambda c: _g(c, "forecast", "macro_regime", "hy_oas")),
    "recession_prob": ("12-month recession probability (fraction)", lambda c: _g(c, "forecast", "macro_regime", "recession_prob_12m")),
    "sahm": ("Sahm rule indicator", lambda c: _g(c, "forecast", "macro_regime", "sahm")),
    "net_liquidity": ("Net liquidity, USD bn", lambda c: (_lever(c, "net_liquidity") or {}).get("level")),
    "sofr_effr": ("SOFR minus EFFR, percentage points", _sofr_effr),
    "srf_takeup": ("Standing Repo Facility take-up, USD bn", lambda c: _repo(c, "repo_bn")),
    "regime": ("Market regime key (CALM, VOL_SHOCK, ...; text)", lambda c: _g(c, "regime", "key")),
    "btc_verdict": ("Bitcoin verdict call (HOLD, BUY (start), ...; text)", lambda c: _g(c, "bitcoin", "verdict", "call")),
    "btc_price": ("Bitcoin price at the last run", lambda c: _g(c, "bitcoin", "price", "price")),
    "btc_realized_price": ("Bitcoin realized price", lambda c: _g(c, "bitcoin", "onchain", "realized_price")),
    "btc_mvrv": ("Bitcoin MVRV", lambda c: _g(c, "bitcoin", "onchain", "mvrv")),
    "spy_vol_control_5d": ("Vol-control flow estimate, last 5 sessions, USD", lambda c: _g(c, "forecast", "SPY", "flows", "vol_control", "flow_last_5d")),
    "spy_net_gex": ("SPY dealer net gamma, USD per 1 point", lambda c: _g(c, "forecast", "SPY", "flows", "dealer_gamma", "net_gex_usd_per_1pt")),
}
CATALOG = {m[0]: m[1] for m in catalog.METRICS}          # catalog id -> dotted path in the snapshot


def metric_list():
    """For the editor's autocomplete: short names and catalog ids with a description."""
    return ([{"id": k, "what": v[0], "kind": "snapshot"} for k, v in METRICS.items()]
            + [{"id": k, "what": f"catalog metric ({p})", "kind": "catalog"} for k, p in CATALOG.items()])


# ============================================================ expressions
_NUM = r"\d+(?:\.\d+)?(?![A-Za-z0-9.^=])"
_TOKEN = re.compile(r"\s*(?:(?P<num>" + _NUM + r")|(?P<met>[a-z][A-Za-z0-9_.]*)|(?P<sym>\^?[A-Z0-9][A-Z0-9.=]*(?:-[A-Z0-9.=]+)*)|(?P<op>[()+\-*/]))")


def parse(expr):
    """-> (ast over placeholders v0.., [operand names in order of first use], normalised text) or ValueError."""
    expr = " ".join(str(expr or "").split())
    if not expr or len(expr) > 200:
        raise ValueError("the expression must be 1 to 200 characters")
    pos, ops, code, norm = 0, [], [], []
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise ValueError(f"cannot read the expression at {expr[pos:pos + 12]!r}")
        pos = m.end()
        if m.group("op") or m.group("num"):
            tok = m.group("op") or m.group("num")
            code.append(tok), norm.append(tok)
            continue
        name = m.group("met") or m.group("sym")
        if m.group("met") and name not in METRICS and name not in CATALOG:
            raise ValueError(f"unknown metric {name!r} (see the metric list in the editor)")
        if name not in ops:
            ops.append(name)
        code.append(f"v{ops.index(name)}"), norm.append(name)
    if not ops:
        raise ValueError("the expression needs at least one symbol or metric")
    if len(ops) > 6:
        raise ValueError("at most 6 symbols and metrics per expression")
    try:
        tree = ast.parse(" ".join(code), mode="eval")
    except SyntaxError:
        raise ValueError("the expression is not valid: check the operators and parentheses")
    formulas._check(tree)
    return tree, ops, " ".join(norm)


def is_text_metric(name):
    return name in ("chaos_regime", "regime", "btc_verdict") or (name in CATALOG and name.endswith((".tier", ".bias", ".status", ".condition", ".regime")))


# ============================================================ values
class Env:
    """What an evaluation reads. ctx: the latest live snapshot (None with `ctx_why` when there is none or it came from an offline run);
    quote(sym): the live last price, or None."""
    def __init__(self, ctx=None, ctx_why=None, quote=None, run_id=None):
        self.ctx, self.ctx_why, self.quote, self.run_id = ctx, ctx_why, quote, run_id
        self._q = {}

    def value(self, name):
        """(value, error). A symbol is its live price, a metric the snapshot's value."""
        if name in METRICS or name in CATALOG:
            if self.ctx is None:
                return None, f"{name}: {self.ctx_why or 'no snapshot'}"
            v = METRICS[name][1](self.ctx) if name in METRICS else _g(self.ctx, *CATALOG[name].split("."))
            if v is None:
                return None, f"{name} is not in the latest run"
            return v, None
        if name not in self._q:
            try:
                self._q[name] = self.quote(name) if self.quote else None
            except Exception:
                self._q[name] = None
        px = self._q[name]
        return (px, None) if px is not None and math.isfinite(px) else (None, f"no live quote for {name}")


def value_of(expr, env):
    """(number or text or None, error or None). A single operand may be text (regime, verdict); anything else must be numeric."""
    try:
        tree, ops, _ = parse(expr)
    except ValueError as e:
        return None, str(e)
    vals = {}
    for o in ops:
        v, err = env.value(o)
        if err:
            return None, err
        vals[o] = v
    if isinstance(tree.body, ast.Name):
        return vals[ops[0]], None
    try:
        nums = {f"v{i}": np.float64(float(vals[o])) for i, o in enumerate(ops)}
    except (TypeError, ValueError):
        return None, "a text metric cannot be used in arithmetic"
    with np.errstate(all="ignore"):
        v = float(formulas._ev(tree, nums))
    return (v, None) if math.isfinite(v) else (None, "the result is not a finite number (division by zero?)")


def _cmp(v, op, t):
    return {"<": v < t, "<=": v <= t, ">": v > t, ">=": v >= t, "==": v == t}[op]


def check_condition(cond, env):
    """One condition -> {value, met, error, gap_pct, progress}. met is None when the value could not be had (or for `changes`,
    which only the job can judge against what it saw last)."""
    v, err = value_of(cond["expr"], env)
    out = {"expr": cond["expr"], "op": cond["op"], "threshold": cond.get("value"), "value": v, "met": None, "error": err, "gap_pct": None, "progress": None}
    if err or cond["op"] == "changes":
        return out
    if isinstance(v, str):
        out["met"] = bool(v == cond.get("value")) if cond["op"] == "==" else None
        out["error"] = None if cond["op"] == "==" else "text values only compare with =="
        return out
    t = float(cond["value"])
    out["met"] = bool(_cmp(v, cond["op"], t))
    if t:
        out["gap_pct"] = round((v - t) / abs(t) * 100, 2)
        out["progress"] = 1.0 if out["met"] else round(max(0.0, 1 - abs(out["gap_pct"]) / BAR_SCALE_PCT), 3)
    return out


def check(tracker, env):
    """All conditions of a tracker: {conditions: [...], met: bool|None, error}. met is None if any value is missing and none is False."""
    cs = [check_condition(c, env) for c in tracker["conditions"]]
    mets = [c["met"] for c in cs]
    met = False if False in mets else None if None in mets else True
    return {"conditions": cs, "met": met, "error": next((c["error"] for c in cs if c["error"]), None)}


# ============================================================ validation and storage
def clean(raw, existing_id=None):
    """A tracker dict from user input, or ValueError with the reason."""
    if not isinstance(raw, dict):
        raise ValueError("a tracker must be an object")
    name = " ".join(str(raw.get("name") or "").split())[:80]
    if not name:
        raise ValueError("a tracker needs a name")
    conds = raw.get("conditions")
    if not isinstance(conds, list) or not 1 <= len(conds) <= MAX_CONDITIONS:
        raise ValueError(f"1 to {MAX_CONDITIONS} conditions")
    out_c = []
    for c in conds:
        op = str((c or {}).get("op") or "")
        if op not in OPS:
            raise ValueError(f"comparator must be one of {', '.join(OPS)}")
        _, ops, norm = parse((c or {}).get("expr"))
        if op == "changes":
            if len(conds) > 1:
                raise ValueError("'changes' must be the only condition")
            out_c.append({"expr": norm, "op": op})
            continue
        val = c.get("value")
        if isinstance(val, str) and val.strip() and op == "==" and len(ops) == 1 and is_text_metric(ops[0]):
            val = val.strip()
        else:
            try:
                val = float(val)
            except (TypeError, ValueError):
                raise ValueError(f"threshold for {norm!r} must be a number")
            if not math.isfinite(val):
                raise ValueError("threshold must be a finite number")
        out_c.append({"expr": norm, "op": op, "value": val})
    pr = str(raw.get("priority") or "default").lower()
    if pr not in PRIORITIES:
        raise ValueError(f"priority must be one of {', '.join(PRIORITIES)}")
    try:
        cool = int(raw.get("cooldown_min", 60))
    except (TypeError, ValueError):
        raise ValueError("cooldown must be a whole number of minutes")
    if not 0 <= cool <= MAX_COOLDOWN_MIN:
        raise ValueError(f"cooldown must be 0 to {MAX_COOLDOWN_MIN} minutes")
    hours = str(raw.get("hours") or "24/7")
    if hours not in HOURS:
        raise ValueError("hours must be '24/7' or 'session'")
    tid = existing_id or str(raw.get("id") or "") or "t." + uuid.uuid4().hex[:8]
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", tid):
        raise ValueError("bad tracker id")
    return {"id": tid, "name": name, "conditions": out_c, "priority": pr, "cooldown_min": cool, "hours": hours,
            "note": " ".join(str(raw.get("note") or "").split())[:300], "enabled": bool(raw.get("enabled", True))}


def _seed(tid, name, conds, priority, hours, cooldown, note):
    return {"id": tid, "name": name, "conditions": conds, "priority": priority, "cooldown_min": cooldown, "hours": hours, "note": note, "enabled": True}


SEEDS = [
    _seed("seed.tranche1", "1st tranche: VIX curve back under 1.0", [{"expr": "^VIX / ^VIX3M", "op": "<", "value": 1.0}], "urgent", "session", 240,
          "Live VIX over live VIX3M. Below 1.0 the curve is back to normal (no inversion)."),
    _seed("seed.tranche2", "2nd tranche: Chaos < 50 and HY OAS < 4%", [{"expr": "chaos", "op": "<", "value": 50}, {"expr": "hy_oas", "op": "<", "value": 4}], "high", "24/7", 720,
          "Both from the latest pipeline run (3 a day), so this can only change when a run commits."),
    _seed("seed.stop", "STOP: HY OAS > 4.5%", [{"expr": "hy_oas", "op": ">", "value": 4.5}], "urgent", "24/7", 720,
          "Credit stress: hold cash. From the latest run (FRED daily)."),
    _seed("seed.regime", "Regime change", [{"expr": "regime", "op": "changes"}], "urgent", "24/7", 0,
          "R · Market Regime moved to a new regime at the latest run; the alert carries the new regime and the one-line read."),
    _seed("seed.btcverdict", "BTC verdict changes", [{"expr": "btc_verdict", "op": "changes"}], "high", "24/7", 0,
          "The BITCOIN tab's HOLD / BUY / SELL / NO CALL call changed at the latest run."),
    _seed("seed.btcrealized", "BTC below realized price", [{"expr": "BTC-USD / btc_realized_price", "op": "<", "value": 1.0}], "high", "24/7", 720,
          "Live BTC-USD over the realized price from the last run's Coin Metrics read. Below 1 = the average coin is under water."),
    _seed("seed.silver3671", "Silver (SLV) at or below $36.71 (tested buy signal)", [{"expr": "SLV", "op": "<=", "value": 36.71}], "high", "session", 1440,
          "SLV live price. The $36.71 level is the tested buy signal."),
]


def store_path():
    """(path, warning). config.TRACKERS_FILE when its folder exists or its parent volume does; else the data folder, with a warning
    that is also logged. Never creates a folder under a volume that is not mounted (/Volumes/<name>/...)."""
    want = config.TRACKERS_FILE
    if want:
        want = Path(want).expanduser()
        try:
            if not want.parent.exists() and want.parent.parent.exists():
                want.parent.mkdir(parents=True, exist_ok=True)
            if want.parent.is_dir():
                return want, None
        except OSError:
            pass
        warn = f"TRACKERS_FILE folder {want.parent} is not available (is the disk mounted?): using the data folder instead"
        log.warning(warn)
        return config.DATA_DIR / "trackers.json", warn
    return config.DATA_DIR / "trackers.json", None


def _beside(name):
    return store_path()[0].with_name(name)


def load(path=None):
    """The saved trackers, or the seeds when nothing has been saved yet."""
    p = path or store_path()[0]
    try:
        data = json.loads(Path(p).read_text())
        return [clean(t, t.get("id")) for t in data.get("trackers", [])]
    except FileNotFoundError:
        return [dict(s, conditions=[dict(c) for c in s["conditions"]]) for s in SEEDS]
    except (ValueError, OSError) as e:
        log.error("trackers file unreadable (%s): %s", p, e)
        raise


def save(trackers, path=None):
    p = Path(path or store_path()[0])
    if len(trackers) > MAX_TRACKERS:
        raise ValueError(f"at most {MAX_TRACKERS} trackers")
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "trackers": trackers}, indent=1))
    tmp.replace(p)


def upsert(raw, path=None):
    items = load(path)
    t = clean(raw, (raw or {}).get("id") if isinstance(raw, dict) else None)
    save([t if x["id"] == t["id"] else x for x in items] + ([] if any(x["id"] == t["id"] for x in items) else [t]), path)
    return t


def delete(tid, path=None):
    items = load(path)
    keep = [t for t in items if t["id"] != tid]
    if len(keep) == len(items):
        raise KeyError(tid)
    save(keep, path)


def duplicate(tid, path=None):
    items = load(path)
    src = next((t for t in items if t["id"] == tid), None)
    if src is None:
        raise KeyError(tid)
    copy = clean({**src, "id": None, "name": f"{src['name']} (copy)"[:80], "enabled": False})
    save(items + [copy], path)
    return copy


# ============================================================ state, history, evaluation
def load_state(path=None):
    try:
        return json.loads(Path(path or _beside("trackers_state.json")).read_text())
    except (OSError, ValueError):
        return {}


def save_state(state, path=None):
    p = Path(path or _beside("trackers_state.json"))
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(p)


def log_fire(entry, path=None):
    with open(path or _beside("trackers_fires.jsonl"), "a") as f:
        f.write(json.dumps(entry) + "\n")


def history(limit=40, path=None):
    """The newest `limit` fires, newest first."""
    try:
        lines = Path(path or _beside("trackers_fires.jsonl")).read_text().splitlines()
    except OSError:
        return []
    out = []
    for ln in reversed(lines[-limit:]):
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def _iso(now):
    return now.astimezone(timezone.utc).isoformat(timespec="seconds")


def _ago_min(iso, now):
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds() / 60
    except (TypeError, ValueError):
        return None


def status_of(tracker, st, now):
    """disabled / FIRED (condition true now) / cooling down (fired inside the cooldown) / armed."""
    if not tracker["enabled"]:
        return "disabled"
    if st.get("met") is True:
        return "FIRED"
    ago = _ago_min(st.get("last_fired"), now)
    return "cooling down" if ago is not None and ago < tracker["cooldown_min"] else "armed"


def message(tracker, res, env, test=False):
    """(title, text) of the push: the conditions with their values, the note, and for a regime tracker the new regime and its read."""
    lines = []
    for c in res["conditions"]:
        v = c["value"]
        shown = "missing" if v is None else v if isinstance(v, str) else f"{v:,.4g}" if abs(v) < 1e5 else f"{v:,.0f}"
        lines.append(f"{c['expr']} = {shown}" + ("" if c["op"] == "changes" else f" ({c['op']} {c['threshold']})"))
    reg = _g(env.ctx, "regime") if env.ctx else None
    if reg and any("regime" in c["expr"].split() for c in res["conditions"]) and reg.get("regime"):
        lines.append(f"Regime now: {reg['regime']}" + (f" (was {reg['previous']['regime']})" if reg.get("previous") else ""))
        if (reg.get("read") or {}).get("status") == "fresh":
            lines.append(f"Read: {reg['read']['text']}")
    if tracker["note"]:
        lines.append(tracker["note"])
    if env.ctx_why is None and env.run_id:
        lines.append(f"Snapshot: run {env.run_id}")
    return ("TEST · " if test else "") + tracker["name"], "\n".join(lines)


def run_due(trackers, env, state, now, send, in_session, log_path=None):
    """Evaluate every enabled tracker whose hours apply. Edge-triggered: fires when its condition turns true (or, for `changes`, when
    the value differs from the last one seen), at most once per cooldown. The first evaluation only records. A push that is not
    delivered records nothing, so the next run sends it again. Returns (new state, [fired tracker ids]); the caller saves the state.
    send(title, text, priority_int, tags) -> True when delivered."""
    state, fired = {k: dict(v) for k, v in state.items()}, []
    for t in trackers:
        if not t["enabled"] or (t["hours"] == "session" and not in_session):
            continue
        st = state.setdefault(t["id"], {})
        res = check(t, env)
        st["checked"], st["error"] = _iso(now), res["error"]
        st["values"] = [c["value"] for c in res["conditions"]]
        changes = t["conditions"][0]["op"] == "changes"
        if res["error"] and (changes or res["met"] is None):
            continue                                   # no reading: not a zero, and neither a fire nor a re-arm
        key, now_v = ("last", res["conditions"][0]["value"]) if changes else ("met", res["met"])
        go = (key in st and st[key] != now_v) if changes else (now_v is True and st.get(key) is False)
        ago = _ago_min(st.get("last_fired"), now)
        if go and not (ago is not None and ago < t["cooldown_min"]):
            title, text = message(t, res, env)
            if not send(title, text, PRIORITIES[t["priority"]], TAGS[PRIORITIES[t["priority"]]]):
                continue                               # undelivered: keep the old state so the next run sees the crossing again
            st["last_fired"] = _iso(now)
            fired.append(t["id"])
            log_fire({"t": _iso(now), "id": t["id"], "name": t["name"], "priority": t["priority"], "text": text, "test": False}, log_path)
        st[key] = now_v                                # first look, no change, a consumed crossing inside the cooldown, or a fire
    return state, fired


def fire_test(tracker, env, send, now, log_path=None):
    """Send the tracker's push marked TEST (no state change). Returns (delivered, title, text)."""
    title, text = message(tracker, check(tracker, env), env, test=True)
    ok = bool(send(title, text, PRIORITIES[tracker["priority"]], TAGS[PRIORITIES[tracker["priority"]]]))
    if ok:
        log_fire({"t": _iso(now), "id": tracker["id"], "name": tracker["name"], "priority": tracker["priority"], "text": text, "test": True}, log_path)
    return ok, title, text


def send_ntfy(title, text, priority, tags):
    """The real sender: one push through send_email.ntfy_push (NTFY_URL). True when it was delivered. Tests pass their own."""
    from send_email import ntfy_push
    return ntfy_push([(title, text, priority, tags)])[0].get("status") == "sent"


def snapshot_env(conn, quote):
    """An Env over the latest committed snapshot. An offline run's snapshot is not used (its numbers are fixtures), so its metrics are errors."""
    from core import lake
    try:
        row = conn.execute("SELECT run_id FROM v2_latest_snapshot").fetchone()
    except Exception:
        row = None
    if not row:
        return Env(None, "no committed run yet", quote)
    ctx = lake.load_snapshot(conn, row[0])
    mode = (ctx.get("run") or {}).get("mode")
    if mode != "live":
        return Env(None, f"the latest run is {mode or 'unknown'}, not a live run", quote, row[0])
    return Env(ctx, None, quote, row[0])
