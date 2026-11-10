"""CHART tab formulas: synthetic charts over tracked symbols and catalog series, e.g. `GC=F / SI=F` or `(HO=F * 42) - CL=F`.

Saved in chart.db v2_chart_formulas (id `f.` + 8 hex of the normalised expression, INSERT OR IGNORE; the first name given stays) and computed on read
from the stored bars and series; nothing is stored of the result except its fans (core/chartfans.py). The text is never
eval'd: operands are tokenised here, replaced by placeholders and the result must be an `ast` of + - * /, unary minus, numbers
and placeholders only. Put spaces around a binary minus: `-` is also a symbol character (BTC-USD, DX-Y.NYB).
"""
import ast
import hashlib
import re
import sqlite3

import numpy as np
import pandas as pd

from core import bars, lake

MAX_OPERANDS = 8
FILL_DAYS = 10                           # a series (EIA weekly) is carried onto the bars' dates at most this far
ID_RE = re.compile(r"^f\.[0-9a-f]{8}$")
_NUM = r"\d+(?:\.\d+)?(?![A-Za-z0-9.^=])"
_SYM = r"(?P<sym>[A-Za-z0-9.^=]*[A-Za-z^][A-Za-z0-9.^=]*(?:-[A-Za-z0-9.^=]+)*)"
_SIDS = "|".join(re.escape(s) for s in sorted(bars.SERIES, key=len, reverse=True))
TOKEN = re.compile(rf"\s*(?:(?P<sid>{_SIDS})(?![A-Za-z0-9.^=:_\-])|(?P<num>{_NUM})|{_SYM}|(?P<op>[()+\-*/]))")


class UnknownOperand(ValueError):
    def __init__(self, names):
        super().__init__("unknown symbol or series: " + ", ".join(names) + " (a symbol must be tracked first, see /api/chart/catalog)")
        self.names = names


def _check(n):
    if isinstance(n, ast.Expression):
        _check(n.body)
    elif isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
        _check(n.left), _check(n.right)
    elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
        _check(n.operand)
    elif not ((isinstance(n, ast.Constant) and type(n.value) in (int, float)) or (isinstance(n, ast.Name) and re.fullmatch(r"v\d+", n.id))):
        raise ValueError("only + - * / ( ), numbers, symbols and series ids are allowed")


def parse(expr):
    """-> (ast over placeholders v0.., [operands in order of first use], normalised text) or ValueError."""
    expr = " ".join(str(expr or "").split())
    if not expr or len(expr) > 200:
        raise ValueError("the expression must be 1 to 200 characters")
    pos, ops, code, norm = 0, [], [], []
    while pos < len(expr):
        m = TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise ValueError(f"cannot read the expression at {expr[pos:pos + 12]!r}")
        pos = m.end()
        if m.group("op") or m.group("num"):
            tok = m.group("op") or m.group("num")
            code.append(tok), norm.append(tok)
            continue
        name = m.group("sid") or m.group("sym").upper()
        if not m.group("sid") and not bars.SYMBOL_RE.match(name):
            raise ValueError(f"{name!r} is not a symbol")
        if name not in ops:
            ops.append(name)
        code.append(f"v{ops.index(name)}"), norm.append(name)
    if not ops:
        raise ValueError("the expression needs at least one symbol or series")
    if len(ops) > MAX_OPERANDS:
        raise ValueError(f"at most {MAX_OPERANDS} symbols and series per formula")
    try:
        tree = ast.parse(" ".join(code), mode="eval")
    except SyntaxError:
        raise ValueError("the expression is not valid: check the operators and parentheses")
    _check(tree)
    return tree, ops, " ".join(norm)


def formula_id(norm):
    return "f." + hashlib.sha1(norm.encode()).hexdigest()[:8]


def save(conn, expr, name=""):
    """Validate and store (INSERT OR IGNORE). Returns the formula row. ValueError for a bad expression, UnknownOperand
    when a symbol is not tracked."""
    _, ops, norm = parse(expr)
    have = set(bars.tracked(conn))
    unknown = [o for o in ops if o not in bars.SERIES and o not in have]
    if unknown:
        raise UnknownOperand(unknown)
    fid, text = formula_id(norm), " ".join(str(expr).split())
    with conn:
        conn.execute("INSERT OR IGNORE INTO v2_chart_formulas VALUES (?,?,?,?)", (fid, " ".join(str(name or "").split())[:60], text, lake.utc_now_iso()))
    return get(conn, fid)


def get(conn, fid):
    return next((f for f in all_formulas(conn) if f["id"] == fid), None)


def all_formulas(conn):
    try:
        return [dict(r) for r in conn.execute("SELECT id, name, expr FROM v2_chart_formulas ORDER BY added_at, id")]
    except sqlite3.OperationalError:               # a chart.db from before formulas: the first bars job or save creates the table
        return []


def describe(f):
    return f"{f['name']} = {f['expr']}" if f["name"] else f["expr"]


def catalog(conn, symbols, series):
    """The catalog entries; first/last are where every operand has data (the dates of the catalog's symbols and series)."""
    have = {x["symbol"]: x for x in symbols} | {x["id"]: x for x in series}
    out = []
    for f in all_formulas(conn):
        try:
            ops = [have.get(o) for o in parse(f["expr"])[1]]
        except ValueError:                     # stored earlier, no longer parses (e.g. a renamed series): listed, no dates
            ops = [None]
        ok = all(o and o["first"] and o["last"] for o in ops)
        out.append({"id": f["id"], "name": f["name"], "expr": f["expr"], "description": describe(f),
                    "first": max(o["first"] for o in ops) if ok else None, "last": min(o["last"] for o in ops) if ok else None})
    return out


def _ev(n, env):
    if isinstance(n, ast.Expression):
        return _ev(n.body, env)
    if isinstance(n, ast.Constant):
        return np.float64(n.value)
    if isinstance(n, ast.Name):
        return env[n.id]
    if isinstance(n, ast.UnaryOp):
        return -_ev(n.operand, env)
    a, b = _ev(n.left, env), _ev(n.right, env)
    return a + b if isinstance(n.op, ast.Add) else a - b if isinstance(n.op, ast.Sub) else a * b if isinstance(n.op, ast.Mult) else a / b


def compute(conn, main, expr, tf, start=None, end=None, limit=20000):
    """The formula as candles: (frame open/high/low/close with a UTC index, None) or (None, reason). `main` is the read-only
    portfolio.db connection for series operands (None: formulas with series are missing). Candles are approximate: open and
    close are the formula of the opens and closes, high and low the formula of the highs and lows widened to include them.
    A time where an operand has no bar, or the result is not a finite number (division by zero), has no candle."""
    tree, ops, _ = parse(expr)
    frames, sers = {}, {}
    for o in ops:
        if o in bars.SERIES:
            if main is None:
                return None, f"{o} is read from portfolio.db, which is not available"
            try:
                s = bars.SERIES[o][4](main)
            except Exception:
                s = pd.Series(dtype=float)
            if s.empty:
                return None, f"no stored values for {o}"
            sers[o] = pd.Series(s.values, index=pd.DatetimeIndex(s.index).tz_localize("UTC"))
        else:
            df = bars.read_bars(conn, o, tf, start, end, limit)
            if df.empty:
                return None, f"no {tf} bars stored for {o}"
            frames[o] = df
    if frames:
        idx = None
        for df in frames.values():
            idx = df.index if idx is None else idx.intersection(df.index)
    elif bars.INTRADAY_RE.match(tf):
        return None, "a formula of series only has daily or coarser points"
    else:
        idx = None
        for s in sers.values():
            idx = s.index if idx is None else idx.intersection(s.index)
        lo, hi = bars._utc(start), bars._utc(end)
        idx = idx[(idx >= lo) if lo is not None else slice(None)]
        idx = idx[(idx <= hi) if hi is not None else slice(None)]
    if idx is None or not len(idx):
        return None, "no time has a bar of every symbol in the formula"
    cols = {}
    with np.errstate(all="ignore"):
        filled = {o: bars.asof(idx, sers[o], FILL_DAYS) for o in ops if o not in frames}      # once, not per column
        for c in ("open", "high", "low", "close"):
            env = {f"v{i}": frames[o][c].reindex(idx) if o in frames else filled[o] for i, o in enumerate(ops)}
            cols[c] = _ev(tree, env)
    out = pd.DataFrame(cols, index=idx).replace([np.inf, -np.inf], np.nan)
    out["high"] = out[["open", "high", "close"]].max(axis=1, skipna=False)
    out["low"] = out[["open", "low", "close"]].min(axis=1, skipna=False)
    out = out.dropna().tail(limit)
    return (out, None) if len(out) else (None, "no time has every operand and a finite result")
