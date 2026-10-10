"""v2 data lake inside the existing portfolio.db.

Additive only: CREATE ... IF NOT EXISTS and ALTER TABLE ADD COLUMN. Nothing here drops, replaces
or deletes existing tables, columns or rows. The one exception is the view v2_latest_snapshot, which holds no
data: migrate() replaces it when its stored definition differs from LATEST_SNAPSHOT_VIEW_SQL. All writes go
through one process-wide lock and short transactions; no transaction is held open across network requests.
"""
import hashlib
import json
import math
import sqlite3
import threading
import zlib
from datetime import date, datetime, timezone

SCHEMA_VERSION = "v2.1"
_WRITE_LOCK = threading.RLock()

SIGNALS_DDL = """CREATE TABLE IF NOT EXISTS v2_trade_signals (
    signal_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    underlying TEXT NOT NULL,
    position_type TEXT NOT NULL,          -- CALL | PUT | CASH
    contract TEXT,
    strike REAL,
    expiration TEXT,
    entry_bid REAL,
    entry_ask REAL,
    entry_last REAL,
    entry_mid REAL,
    entry_iv REAL,
    entry_volume REAL,
    entry_oi REAL,
    underlying_price REAL,
    score INTEGER,
    bias TEXT,
    allocation TEXT,
    rationale TEXT,
    starred INTEGER NOT NULL DEFAULT 0,
    deleted_at TEXT,
    source TEXT NOT NULL DEFAULT 'engine_run'   -- engine_run | email_archive
)"""

FORECASTS_DDL = """CREATE TABLE IF NOT EXISTS v2_forecasts (
    forecast_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    model TEXT NOT NULL,
    horizon TEXT NOT NULL,
    target_date TEXT NOT NULL,
    spot REAL,
    p_up REAL,
    lower68 REAL,
    upper68 REAL,
    direction TEXT,
    vol REAL,
    UNIQUE(run_id, symbol, model, horizon)
)"""

WATCH_DDL = """CREATE TABLE IF NOT EXISTS v2_watch_positions (
    position_id INTEGER PRIMARY KEY,
    opened_on TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    rule TEXT NOT NULL,
    underlying TEXT NOT NULL,
    underlying_price REAL,
    expiration TEXT,
    long_strike REAL,
    short_strike REAL,
    long_contract TEXT,
    short_contract TEXT,
    entry_debit REAL,
    exit_due TEXT NOT NULL,
    closed_at TEXT,
    exit_value REAL,
    exit_underlying REAL,
    UNIQUE(rule, opened_on)
)"""

MIGRATIONS = [
    SIGNALS_DDL,
    FORECASTS_DDL,
    WATCH_DDL,
    """CREATE TABLE IF NOT EXISTS v2_runs (
        run_id TEXT PRIMARY KEY,
        mode TEXT NOT NULL,
        trigger TEXT,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        run_folder TEXT,
        code_version TEXT,
        schema_version TEXT,
        stages_json TEXT DEFAULT '{}',
        sources_json TEXT DEFAULT '{}',
        request_counts_json TEXT DEFAULT '{}',
        artifacts_json TEXT DEFAULT '{}',
        delivery_status TEXT DEFAULT 'not_requested',
        delivery_detail TEXT,
        elapsed_s REAL,
        error TEXT,
        parent_run_id TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS v2_payloads (
        payload_id INTEGER PRIMARY KEY,
        sha256 TEXT NOT NULL UNIQUE,
        source TEXT NOT NULL,
        kind TEXT NOT NULL,
        request_fingerprint TEXT,
        request_json TEXT,
        fetched_at TEXT,
        source_date TEXT,
        format TEXT,
        size_bytes INTEGER,
        compression TEXT,
        content BLOB,
        parsed_blob BLOB,
        coverage_json TEXT,
        status TEXT,
        first_run_id TEXT,
        origin_path TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS v2_fetch_log (
        fetch_id INTEGER PRIMARY KEY,
        run_id TEXT,
        source TEXT NOT NULL,
        request_fingerprint TEXT,
        request_json TEXT,
        started_at TEXT,
        elapsed_ms INTEGER,
        outcome TEXT NOT NULL,
        http_status INTEGER,
        payload_id INTEGER,
        detail TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS v2_observations (
        obs_id INTEGER PRIMARY KEY,
        identity TEXT NOT NULL UNIQUE,
        metric_id TEXT NOT NULL,
        entity TEXT NOT NULL DEFAULT '',
        dims_json TEXT NOT NULL DEFAULT '{}',
        observed_at TEXT NOT NULL,
        observed_tz TEXT,
        time_quality TEXT,
        value_num REAL,
        value_text TEXT,
        value_json TEXT,
        unit TEXT,
        status TEXT NOT NULL,
        reason TEXT,
        source TEXT NOT NULL,
        source_field TEXT,
        payload_id INTEGER,
        run_id TEXT,
        fetched_at TEXT,
        derivation TEXT,
        inputs_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_v2_obs_metric ON v2_observations(metric_id, entity, observed_at)",
    "CREATE INDEX IF NOT EXISTS ix_v2_obs_run ON v2_observations(run_id)",
    """CREATE TABLE IF NOT EXISTS v2_snapshots (
        snapshot_id INTEGER PRIMARY KEY,
        run_id TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        schema_version TEXT,
        context_json TEXT NOT NULL,
        context_sha256 TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_v2_snap_time ON v2_snapshots(created_at)",
    """CREATE TRIGGER IF NOT EXISTS v2_snapshots_immutable BEFORE UPDATE ON v2_snapshots
        BEGIN SELECT RAISE(ABORT, 'v2_snapshots rows are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS v2_snapshots_no_delete BEFORE DELETE ON v2_snapshots
        BEGIN SELECT RAISE(ABORT, 'v2_snapshots rows cannot be deleted'); END""",
    """CREATE TABLE IF NOT EXISTS v2_imports (
        import_id INTEGER PRIMARY KEY,
        path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        size_bytes INTEGER,
        mtime TEXT,
        kind TEXT,
        parser_version TEXT NOT NULL,
        status TEXT NOT NULL,
        rows_imported INTEGER,
        payload_id INTEGER,
        result_json TEXT,
        conflicts_json TEXT,
        imported_at TEXT,
        UNIQUE(path, sha256, parser_version)
    )""",
]


# The snapshot every reader treats as "the latest": the newest committed LIVE run's, and only when no live run has
# a committed snapshot, the newest of any other mode (offline). An offline test run therefore never hides a live one.
LATEST_SNAPSHOT_VIEW_SQL = """CREATE VIEW v2_latest_snapshot AS
        SELECT s.* FROM v2_snapshots s JOIN v2_runs r ON r.run_id = s.run_id
        WHERE r.status IN ('committed', 'completed', 'completed_with_warnings')
        ORDER BY (r.mode = 'live') DESC, s.created_at DESC LIMIT 1"""


import re as _re

_SECRET_PATTERNS = [(_re.compile(r"(?i)(api_key|apikey|token|access_token|x-access-token|password|secret)=([^&\s'\"]+)"),
                     r"\1=REDACTED")]


def redact(text):
    """Remove credentials from any text before it is stored or returned (URLs in error messages carry keys)."""
    if text is None:
        return None
    out = str(text)
    for pat, rep in _SECRET_PATTERNS:
        out = pat.sub(rep, out)
    return out


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path, readonly=False):
    if readonly:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    else:
        conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row
    return conn


def connect_readonly(db_path):
    """Read-only connection (`mode=ro`) with the same row factory as `connect()`. It cannot create or change
    anything: opening a database file that does not exist raises sqlite3.OperationalError, and so does reading a
    table that has not been created yet (the pipeline's `migrate()` creates them). Readers that must not write
    (the terminal's position and forecast reads) use this and never call `migrate()`."""
    return connect(db_path, readonly=True)


def _squash(sql):
    return " ".join(str(sql or "").split())


def _ensure_latest_snapshot_view(conn):
    """Create v2_latest_snapshot, or replace it when the stored definition differs from LATEST_SNAPSHOT_VIEW_SQL.
    A view holds no data, so replacing one is the only non-additive step here. It is atomic: readers see the old
    or the new definition, never none."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'view' AND name = 'v2_latest_snapshot'").fetchone()
    if row is not None and _squash(row[0]) == _squash(LATEST_SNAPSHOT_VIEW_SQL):
        return
    with conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP VIEW IF EXISTS v2_latest_snapshot")
        conn.execute(LATEST_SNAPSHOT_VIEW_SQL)


def migrate(conn):
    with _WRITE_LOCK:
        with conn:
            for stmt in MIGRATIONS:
                conn.execute(stmt)
            if "source" not in table_columns(conn, "v2_trade_signals"):   # additive column for existing installs
                conn.execute("ALTER TABLE v2_trade_signals ADD COLUMN source TEXT NOT NULL DEFAULT 'engine_run'")
        _ensure_latest_snapshot_view(conn)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _json_default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if hasattr(o, "item"):
        return o.item()
    if isinstance(o, bytes):
        return {"sha256": sha256_bytes(o), "len": len(o)}
    return str(o)


def clean_json(value):
    """JSON-safe copy: NaN/inf -> None (missing is null, never a fake number)."""
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, dict):
        return {str(k): clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json(v) for v in value]
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return clean_json(value.item())
        except Exception:
            return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def dumps(value, **kw):
    return json.dumps(clean_json(value), default=_json_default, sort_keys=True, **kw)


def compress(data):
    return zlib.compress(data, 6)


def decompress(blob):
    return zlib.decompress(blob) if blob is not None else None


# ---------------------------------------------------------------- payloads
def store_payload(conn, *, source, kind, content, fmt, request=None, fetched_at=None, source_date=None,
                  parsed=None, coverage=None, status="ok", run_id=None, origin_path=None):
    """Lossless capture. Deduplicated by content hash; returns payload_id."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    if fmt in ("xml", "json", "csv", "text", "html", "eml") and b"=" in content:
        cleaned = redact(content.decode("utf-8", errors="surrogateescape")).encode("utf-8", errors="surrogateescape")
        if cleaned != content:
            content = cleaned
            coverage = dict(coverage or {}, redacted_credentials=True)
    digest = sha256_bytes(content)
    with _WRITE_LOCK:
        row = conn.execute("SELECT payload_id FROM v2_payloads WHERE sha256 = ?", (digest,)).fetchone()
        if row:
            return row["payload_id"]
        use_zlib = fmt not in ("xlsx", "png", "zip", "gzip")
        blob = compress(content) if use_zlib else content
        parsed_blob = compress(dumps(parsed).encode()) if parsed is not None else None
        request_json = dumps(request) if request is not None else None
        fingerprint = sha256_bytes(request_json.encode())[:24] if request_json else None
        with conn:
            cur = conn.execute(
                """INSERT INTO v2_payloads (sha256, source, kind, request_fingerprint, request_json, fetched_at,
                   source_date, format, size_bytes, compression, content, parsed_blob, coverage_json, status,
                   first_run_id, origin_path) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (digest, source, kind, fingerprint, request_json, fetched_at or utc_now_iso(), source_date, fmt,
                 len(content), "zlib" if use_zlib else None, blob, parsed_blob,
                 dumps(coverage) if coverage is not None else None, status, run_id,
                 str(origin_path) if origin_path else None),
            )
        return cur.lastrowid


def load_payload(conn, payload_id):
    row = conn.execute("SELECT * FROM v2_payloads WHERE payload_id = ?", (payload_id,)).fetchone()
    if row is None:
        return None, None
    content = decompress(row["content"]) if row["compression"] == "zlib" else row["content"]
    return row, content


def log_fetch(conn, *, run_id, source, request, started_at, elapsed_ms, outcome, http_status=None,
              payload_id=None, detail=None):
    request_json = dumps(request) if request is not None else None
    with _WRITE_LOCK, conn:
        conn.execute(
            """INSERT INTO v2_fetch_log (run_id, source, request_fingerprint, request_json, started_at, elapsed_ms,
               outcome, http_status, payload_id, detail) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (run_id, source, sha256_bytes(request_json.encode())[:24] if request_json else None, request_json,
             started_at, elapsed_ms, outcome, http_status, payload_id, redact(detail or "")[:2000]),
        )


# ---------------------------------------------------------------- observations
def obs(metric_id, observed_at, *, entity="", dims=None, value=None, unit=None, status="fresh", reason=None,
        source, source_field=None, payload_id=None, observed_tz="UTC", time_quality="exact", derivation=None,
        inputs=None):
    """Build one observation record. `value` may be a number, string, dict/list, or None (with a reason)."""
    value_num = value_text = value_json = None
    if isinstance(value, bool):
        value_text = str(value).lower()
    elif isinstance(value, (int, float)) or (hasattr(value, "item") and not isinstance(value, (str, bytes, dict, list))):
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            value_num = None
            status = "missing" if status == "fresh" else status
            reason = reason or "provider returned NaN"
        else:
            value_num = v
    elif isinstance(value, str):
        value_text = value
    elif value is not None:
        value_json = dumps(value)
    if value is None and status == "fresh":
        status = "missing"
    if status == "missing" and not reason:
        reason = "no value supplied"
    return {
        "metric_id": metric_id, "entity": entity or "", "dims_json": dumps(dims or {}),
        "observed_at": observed_at.isoformat() if hasattr(observed_at, "isoformat") else str(observed_at),
        "observed_tz": observed_tz, "time_quality": time_quality, "value_num": value_num, "value_text": value_text,
        "value_json": value_json, "unit": unit, "status": status, "reason": reason, "source": source,
        "source_field": source_field, "payload_id": payload_id, "derivation": derivation,
        "inputs_json": dumps(inputs) if inputs is not None else None,
    }


def _identity(rec):
    # Same metric/entity/dims/time/source/value => same observation, regardless of which run saw it.
    # A revised value at the same time is a distinct observation and is kept.
    key = "|".join(str(rec.get(k)) for k in (
        "metric_id", "entity", "dims_json", "observed_at", "source", "value_num", "value_text", "value_json",
        "status", "derivation"))
    return sha256_bytes(key.encode())


def record_observations(conn, records, *, run_id=None, fetched_at=None):
    """Bulk, idempotent insert. Returns number of new rows."""
    if not records:
        return 0
    fetched_at = fetched_at or utc_now_iso()
    rows = []
    for r in records:
        rows.append((
            _identity(r), r["metric_id"], r["entity"], r["dims_json"], r["observed_at"], r["observed_tz"],
            r["time_quality"], r["value_num"], r["value_text"], r["value_json"], r["unit"], r["status"],
            r["reason"], r["source"], r["source_field"], r["payload_id"], run_id, fetched_at, r["derivation"],
            r["inputs_json"],
        ))
    with _WRITE_LOCK:
        before = conn.total_changes
        with conn:
            conn.executemany(
                """INSERT OR IGNORE INTO v2_observations (identity, metric_id, entity, dims_json, observed_at,
                   observed_tz, time_quality, value_num, value_text, value_json, unit, status, reason, source,
                   source_field, payload_id, run_id, fetched_at, derivation, inputs_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
        return conn.total_changes - before


# ---------------------------------------------------------------- runs & snapshots
def create_run(conn, run_id, *, mode, trigger, run_folder, code_version, parent_run_id=None):
    with _WRITE_LOCK, conn:
        conn.execute(
            """INSERT OR IGNORE INTO v2_runs (run_id, mode, trigger, status, started_at, run_folder, code_version,
               schema_version, parent_run_id) VALUES (?,?,?,?,?,?,?,?,?)""",
            (run_id, mode, trigger, "running", utc_now_iso(), run_folder, code_version, SCHEMA_VERSION,
             parent_run_id),
        )


def update_run(conn, run_id, **fields):
    if not fields:
        return
    cols, vals = [], []
    for k, v in fields.items():
        if k.endswith("_json") and not isinstance(v, str):
            v = dumps(v)
        cols.append(f"{k} = ?")
        vals.append(v)
    vals.append(run_id)
    with _WRITE_LOCK, conn:
        conn.execute(f"UPDATE v2_runs SET {', '.join(cols)} WHERE run_id = ?", vals)


def get_run(conn, run_id):
    row = conn.execute("SELECT * FROM v2_runs WHERE run_id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def commit_snapshot(conn, run_id, context):
    body = dumps(context)
    digest = sha256_bytes(body.encode())
    with _WRITE_LOCK, conn:
        existing = conn.execute("SELECT context_sha256 FROM v2_snapshots WHERE run_id = ?", (run_id,)).fetchone()
        if existing:
            if existing["context_sha256"] != digest:
                raise RuntimeError(f"Snapshot for {run_id} already committed with different content")
            return digest
        conn.execute(
            "INSERT INTO v2_snapshots (run_id, created_at, schema_version, context_json, context_sha256) VALUES (?,?,?,?,?)",
            (run_id, utc_now_iso(), SCHEMA_VERSION, body, digest),
        )
    # Verify it is actually readable before anyone reports success.
    check = conn.execute("SELECT context_sha256 FROM v2_snapshots WHERE run_id = ?", (run_id,)).fetchone()
    if not check or check["context_sha256"] != digest:
        raise RuntimeError("Snapshot commit could not be verified")
    return digest


def load_snapshot(conn, run_id=None):
    if run_id:
        row = conn.execute("SELECT * FROM v2_snapshots WHERE run_id = ?", (run_id,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM v2_latest_snapshot").fetchone()
    if not row:
        return None
    return json.loads(row["context_json"])


# ---------------------------------------------------------------- legacy table compatibility
def table_columns(conn, table):
    return [r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _sql_type(v):
    if isinstance(v, bool):
        return "INTEGER"
    if isinstance(v, int):
        return "INTEGER"
    if isinstance(v, float):
        return "REAL"
    return "TEXT"


def _legacy_value(v):
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, str) and v in ("N/A", "NaN", "nan", ""):
        return None
    if hasattr(v, "item"):
        return v.item()
    return v


def append_legacy_row(conn, table, row):
    """Insert into a legacy table using its real schema. Unknown columns in the table stay untouched;
    new keys are added with ALTER TABLE ADD COLUMN. Missing values are stored as NULL."""
    with _WRITE_LOCK:
        cols = table_columns(conn, table)
        with conn:
            if not cols:
                defs = ", ".join(f'"{k}" {_sql_type(v)}' for k, v in row.items())
                conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({defs})')
                cols = list(row.keys())
            for k, v in row.items():
                if k not in cols:
                    conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{k}" {_sql_type(v)}')
                    cols.append(k)
            keys = list(row.keys())
            conn.execute(
                f'INSERT INTO "{table}" ({", ".join(chr(34) + k + chr(34) for k in keys)}) VALUES ({", ".join("?" for _ in keys)})',
                [_legacy_value(row[k]) for k in keys],
            )


PROTECTED_TABLES = {
    "macro_master_ledger", "equities_darkpool_gex_ledger", "institutional_ledger", "physical_arbitrage_ledger",
    "comex_inventory_history", "daily_volume", "silver_stocks", "crypto_metrics_history",
}


def table_counts(conn):
    out = {}
    for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        out[r["name"]] = conn.execute(f'SELECT COUNT(*) FROM "{r["name"]}"').fetchone()[0]
    return out


def backup_database(db_path, dest_path):
    """Consistent copy using the SQLite backup API."""
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(str(dest_path))
    with dst:
        src.backup(dst)
    ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
    src.close()
    dst.close()
    if ok != "ok":
        raise RuntimeError(f"Backup integrity check failed: {ok}")
    return dest_path
