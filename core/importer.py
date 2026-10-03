"""Idempotent, non-destructive import of the existing installation into the v2 lake.

Old files are read, hashed and captured; they are never moved, renamed or deleted. Filename dates and
embedded report dates are compared and conflicts recorded. Re-running skips files already imported
with the same hash and parser version.
"""
import csv
import io
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from config import DATA_DIR, DB_PATH, PROJECT_ROOT
from core import cme, lake

PARSER_VERSION_LEDGER = "ledger_csv_v1"
PARSER_VERSION_XML = "daily_xml_v2"
PARSER_VERSION_DB = "legacy_db_v1"
PARSER_VERSION_JSON = "json_v1"

# Never captured: browser session cookies / credentials.
EXCLUDED_NAMES = {"state.json", ".DS_Store", ".env"}
LEGACY_TABLE_TIME_COLS = {"macro_master_ledger": "Datetime", "equities_darkpool_gex_ledger": "Date",
                          "institutional_ledger": "Date", "physical_arbitrage_ledger": "Datetime",
                          "comex_inventory_history": "Date", "crypto_metrics_history": "Date",
                          "daily_volume": "source_date", "silver_stocks": "source_date"}
LEDGER_CSVS = {"macro_master_ledger.csv": "Datetime", "equities_darkpool_gex_ledger.csv": "Date",
               "institutional_ledger.csv": "Date", "physical_arbitrage_ledger.csv": "Datetime",
               "comex_inventory_history.csv": "Date", "backup macro_master_ledger copy.csv": "Datetime"}


def parse_legacy_time(value):
    """Legacy ledgers store naive local (Chicago) times in mixed formats. Returns (iso, quality)."""
    if value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() == "":
        return None, "missing"
    s = str(value).strip()
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}", "date_only_from_filename"
    try:
        ts = pd.to_datetime(s, format="mixed")
    except (ValueError, TypeError):
        return None, "unparseable"
    if ts.hour == 0 and ts.minute == 0 and len(s) <= 10:
        return ts.date().isoformat(), "date_only"
    return ts.strftime("%Y-%m-%dT%H:%M:%S"), "naive_local_assumed_chicago"


def _already(conn, path, digest, parser_version):
    with lake._WRITE_LOCK:
        row = conn.execute("SELECT status FROM v2_imports WHERE path=? AND sha256=? AND parser_version=?",
                           (str(path), digest, parser_version)).fetchone()
    return row is not None and row["status"] in ("imported", "duplicate_content", "skipped", "invalid")


def _record_import(conn, path, digest, size, kind, parser_version, status, rows=0, payload_id=None, result=None,
                   conflicts=None):
    mtime = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).isoformat() if os.path.exists(path) else None
    with lake._WRITE_LOCK, conn:
        conn.execute(
            """INSERT OR REPLACE INTO v2_imports (path, sha256, size_bytes, mtime, kind, parser_version, status,
               rows_imported, payload_id, result_json, conflicts_json, imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(path), digest, size, mtime, kind, parser_version, status, rows, payload_id,
             lake.dumps(result or {}), lake.dumps(conflicts or []), lake.utc_now_iso()))


def _row_observations(table, rows, time_col, source, payload_id=None):
    recs = []
    for i, row in enumerate(rows):
        when, quality = parse_legacy_time(row.get(time_col))
        clean = {k: (None if (isinstance(v, float) and pd.isna(v)) else v) for k, v in row.items()}
        dims = {"ticker": clean.get("Ticker")} if "Ticker" in clean else {}
        recs.append(lake.obs(f"legacy.{table}.row", when or f"unknown#{i}", entity=clean.get("Ticker") or "",
                             dims=dims, value=clean, source=source, payload_id=payload_id, observed_tz="America/Chicago",
                             time_quality=quality if when else "unknown", status="historical"))
    return recs


# ---------------------------------------------------------------- per-kind importers
def import_legacy_tables(conn, report):
    for table, time_col in LEGACY_TABLE_TIME_COLS.items():
        cols = lake.table_columns(conn, table)
        if not cols:
            continue
        rows = [dict(r) for r in conn.execute(f'SELECT * FROM "{table}"')]
        tc = time_col if time_col in cols else cols[0]
        added = lake.record_observations(conn, _row_observations(table, rows, tc, f"legacy_db:{table}"))
        report["legacy_tables"][table] = {"rows": len(rows), "new_observations": added}


def import_ledger_csv(conn, path, time_col, report):
    data = path.read_bytes()
    digest = lake.sha256_bytes(data)
    if _already(conn, path, digest, PARSER_VERSION_LEDGER):
        report["skipped_unchanged"] += 1
        return
    text = data.decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    pid = lake.store_payload(conn, source="legacy_csv", kind="ledger_csv", content=data, fmt="csv",
                             origin_path=path, coverage={"rows": len(rows)})
    table = path.stem.replace("backup ", "backup_").replace(" copy", "_copy").replace(" ", "_")
    added = lake.record_observations(conn, _row_observations(table, rows, time_col, f"legacy_csv:{path.name}", pid))
    _record_import(conn, path, digest, len(data), "ledger_csv", PARSER_VERSION_LEDGER, "imported", added, pid,
                   {"rows": len(rows), "header": list(rows[0].keys()) if rows else []})
    report["files"]["ledger_csv"] += 1


def volume_observations(parsed, source, payload_id, report_date=None):
    td = report_date or parsed["trade_date"]
    recs = []
    for key, r in cme.target_rows(parsed).items():
        dims = {"code": r["code"], "fo": r["fo"], "description": r["description"],
                "report_version": parsed.get("report_version")}
        recs.append(lake.obs(f"cme.{key}.volume", td, entity=key, dims=dims, value=r["volume"], unit="contracts",
                             source=source, source_field="Total Volume", payload_id=payload_id,
                             observed_tz="America/Chicago", time_quality="trade_date"))
        recs.append(lake.obs(f"cme.{key}.open_interest", td, entity=key, dims=dims, value=r["open_interest"],
                             unit="contracts", source=source, source_field="Open Interest", payload_id=payload_id,
                             observed_tz="America/Chicago", time_quality="trade_date"))
    summary = next((s for n, s in parsed["sheets"].items() if "BY PRODUCT" not in n.upper()), None)
    if summary:
        for rec in summary["rows"]:
            name = str(rec.get("Description") or "").strip()
            if name:
                recs.append(lake.obs("cme.asset_class.row", td, entity=name, value=rec, source=source,
                                     payload_id=payload_id, observed_tz="America/Chicago", time_quality="trade_date"))
    return recs


def inventory_observations(parsed, source, payload_id):
    rd = parsed["report_date"]
    s = cme.inventory_summary(parsed)
    common = dict(source=source, payload_id=payload_id, observed_tz="America/Chicago", time_quality="report_date",
                  dims={"activity_date": parsed["activity_date"]}, entity="SILVER")
    recs = []
    for field, metric in (("registered", "registered"), ("eligible", "eligible"), ("total", "total"),
                          ("reg_change", "registered_net_change"), ("elig_change", "eligible_net_change"),
                          ("total_change", "total_net_change"), ("reg_adjustment", "registered_adjustment"),
                          ("elig_adjustment", "eligible_adjustment"), ("total_adjustment", "total_adjustment"),
                          ("reg_prev", "registered_prev_total"), ("elig_prev", "eligible_prev_total")):
        recs.append(lake.obs(f"comex.silver.{metric}", rd, value=s[field], unit="troy_oz", **common))
    for dep in parsed["depositories"]:
        recs.append(lake.obs("comex.silver.depository", rd, entity=dep["depository"], value=dep["rows"],
                             unit="troy_oz", source=source, payload_id=payload_id, observed_tz="America/Chicago",
                             time_quality="report_date", dims={"activity_date": parsed["activity_date"]}))
    recs.append(lake.obs("comex.silver.reconciliation", rd, entity="SILVER", value=parsed["checks"], source=source,
                         payload_id=payload_id, status="fresh" if not parsed["warnings"] else "partial",
                         reason="; ".join(parsed["warnings"]) or None, time_quality="report_date"))
    return recs


def _filename_date(name):
    m = re.search(r"(\d{4})(\d{2})(\d{2})", name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(\d{2})-(\d{2})-(\d{2})", name)
    if m:
        return f"20{m.group(3)}-{m.group(1)}-{m.group(2)}"
    return None


def _unchanged_on_disk(conn, path, parser_version):
    """Cheap pre-check: same path, size and mtime as a finished import → skip without reading or hashing."""
    st = path.stat()
    mtime = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
    with lake._WRITE_LOCK:
        row = conn.execute("SELECT status FROM v2_imports WHERE path=? AND size_bytes=? AND mtime=? AND parser_version=?",
                           (str(path), st.st_size, mtime, parser_version)).fetchone()
    return row is not None and row["status"] in ("imported", "duplicate_content", "skipped", "invalid")


def import_workbook(conn, path, kind, report, seen_hashes):
    parser_version = cme.PARSER_VERSION_VOLUME if kind == "volume" else cme.PARSER_VERSION_INVENTORY
    if _unchanged_on_disk(conn, path, parser_version):
        report["skipped_unchanged"] += 1
        return
    data = path.read_bytes()
    digest = lake.sha256_bytes(data)
    if _already(conn, path, digest, parser_version):
        report["skipped_unchanged"] += 1
        return
    fmt = cme.detect_format(data)
    conflicts = []
    try:
        parsed = cme.parse_volume(data) if kind == "volume" else cme.parse_inventory(data)
    except (cme.CMEValidationError, Exception) as e:
        pid = lake.store_payload(conn, source="legacy_file", kind=f"{kind}_workbook_invalid", content=data,
                                 fmt=fmt, origin_path=path, status="invalid")
        _record_import(conn, path, digest, len(data), kind, parser_version, "invalid", 0, pid, {"error": str(e)})
        report["invalid"].append({"path": path.name, "error": str(e)})
        return
    embedded = parsed["trade_date"] if kind == "volume" else parsed["report_date"]
    fname_date = _filename_date(path.name)
    if fname_date and fname_date != embedded:
        conflicts.append({"type": "filename_date_differs", "filename_date": fname_date, "embedded_date": embedded,
                          "resolution": "embedded date used"})
    if digest in seen_hashes:
        conflicts.append({"type": "duplicate_content", "same_as": seen_hashes[digest]})
    seen_hashes.setdefault(digest, path.name)
    pid = lake.store_payload(conn, source=f"cme_{kind}", kind=f"{kind}_workbook", content=data, fmt=fmt,
                             source_date=embedded, parsed=parsed, origin_path=path,
                             coverage={"sheets": list(parsed.get("sheets", {"inventory": 1}).keys())})
    src = f"cme:{'daily_volume' if kind == 'volume' else 'silver_stocks'}"
    recs = volume_observations(parsed, src, pid) if kind == "volume" else inventory_observations(parsed, src, pid)
    added = lake.record_observations(conn, recs)
    status = "duplicate_content" if any(c["type"] == "duplicate_content" for c in conflicts) else "imported"
    _record_import(conn, path, digest, len(data), kind, parser_version, status, added, pid,
                   {"embedded_date": embedded, "format": fmt}, conflicts)
    report["files"][kind] += 1
    if conflicts:
        report["conflicts"].append({"path": path.name, "conflicts": conflicts})


def _xml_to_dict(el):
    node = {"tag": el.tag}
    if el.attrib:
        node["attrs"] = dict(el.attrib)
    text = (el.text or "").strip()
    if text:
        node["text"] = text
    kids = [_xml_to_dict(c) for c in el]
    if kids:
        node["children"] = kids
    return node


def import_daily_file(conn, path, report):
    data = path.read_bytes()
    digest = lake.sha256_bytes(data)
    if _already(conn, path, digest, PARSER_VERSION_XML):
        report["skipped_unchanged"] += 1
        return
    folder = path.parent.name
    try:
        folder_date = datetime.strptime(folder, "%b-%d-%y").date().isoformat()
    except ValueError:
        folder_date = None
    parsed, status, err = None, "imported", None
    fmt = "xml" if path.suffix == ".txt" else path.suffix.lstrip(".")
    if path.suffix in (".txt", ".xml"):
        if not data.lstrip().startswith(b"<"):
            fmt = "text"  # pre-XML plain-text report format (Feb-Apr 2026); captured as-is
            parsed = {"legacy_text": True}
        else:
            try:
                parsed = _xml_to_dict(ET.fromstring(data))
            except ET.ParseError as e:
                status, err = "invalid", f"XML parse error: {e}"
    pid = lake.store_payload(conn, source="legacy_daily_output", kind=path.name, content=data, fmt=fmt,
                             source_date=folder_date, parsed=parsed, origin_path=path,
                             status="ok" if status == "imported" else "invalid")
    _record_import(conn, path, digest, len(data), "daily_output", PARSER_VERSION_XML, status, 0, pid,
                   {"folder_date": folder_date, "error": err})
    report["files"]["daily_output"] += 1
    if err:
        report["invalid"].append({"path": f"{folder}/{path.name}", "error": err})


def import_json_file(conn, path, report, kind):
    data = path.read_bytes()
    digest = lake.sha256_bytes(data)
    if _already(conn, path, digest, PARSER_VERSION_JSON):
        report["skipped_unchanged"] += 1
        return
    try:
        json.loads(data)
        status = "imported"
    except ValueError as e:
        status = "invalid"
    pid = lake.store_payload(conn, source="legacy_json", kind=kind, content=data, fmt="json", origin_path=path,
                             status="ok" if status == "imported" else "invalid")
    _record_import(conn, path, digest, len(data), kind, PARSER_VERSION_JSON, status, 0, pid)
    report["files"]["json"] += 1


# ---------------------------------------------------------------- entry point
def import_history(data_dir=None, db_path=None, verbose=True):
    data_dir = Path(data_dir or DATA_DIR)
    db_path = Path(db_path or DB_PATH)
    conn = lake.connect(db_path)
    lake.migrate(conn)
    before = lake.table_counts(conn)
    report = {"data_dir": str(data_dir), "started_at": lake.utc_now_iso(), "legacy_tables": {},
              "files": {"volume": 0, "inventory": 0, "ledger_csv": 0, "daily_output": 0, "json": 0},
              "skipped_unchanged": 0, "not_imported": [], "invalid": [], "conflicts": []}
    import_legacy_tables(conn, report)
    seen = {r["sha256"]: Path(r["path"]).name for r in
            conn.execute("SELECT sha256, path FROM v2_imports WHERE kind IN ('volume','inventory')")}
    for p in sorted(data_dir.iterdir()):
        if p.name.startswith(".") and p.name not in EXCLUDED_NAMES:
            continue
        if p.name in EXCLUDED_NAMES:
            report["not_imported"].append({"path": p.name, "reason": "excluded (credentials/system file)"})
            continue
        if p.is_dir():
            if re.fullmatch(r"[A-Z][a-z]{2}-\d{2}-\d{2}", p.name):
                for f in sorted(p.iterdir()):
                    if f.suffix in (".txt", ".xml") or f.name == "master_market_data.csv":
                        import_daily_file(conn, f, report)
                    elif f.name in ("report_snapshot.json", "run_manifest.json", "daily_market_report.txt",
                                    "email.eml") or f.suffix in (".png", ".html"):
                        continue  # rendered artifacts; reproducible from snapshots/history
                    else:
                        report["not_imported"].append({"path": f"{p.name}/{f.name}", "reason": "unknown file type"})
            continue
        name = p.name
        if name.startswith("daily_volume") and p.suffix in (".xlsx", ".xls"):
            import_workbook(conn, p, "volume", report, seen)
        elif name.startswith("silver_stocks") and p.suffix in (".xls", ".xlsx"):
            import_workbook(conn, p, "inventory", report, seen)
        elif name in LEDGER_CSVS:
            import_ledger_csv(conn, p, LEDGER_CSVS[name], report)
        elif p.suffix == ".db":
            continue
        elif p.suffix == ".png":
            report["not_imported"].append({"path": name, "reason": "image (not financial data)"})
        else:
            report["not_imported"].append({"path": name, "reason": "unknown file type"})
    # Main project directory financial JSON (minute candles, optional deployment state).
    for name, kind in (("spy_wicks_1m.json", "spy_wicks_1m"), ("deployment_payload.json", "deployment_simulation"),
                       ("deployment_state.json", "deployment_simulation")):
        f = PROJECT_ROOT / name
        if f.exists():
            import_json_file(conn, f, report, kind)
    after = lake.table_counts(conn)
    shrunk = {t: (before[t], after.get(t)) for t in before if after.get(t, 0) < before[t]}
    if shrunk:
        raise RuntimeError(f"Import integrity failure: tables lost rows {shrunk}")
    report["table_counts_before"] = {k: v for k, v in before.items()}
    report["table_counts_after"] = after
    report["finished_at"] = lake.utc_now_iso()
    conn.close()
    if verbose:
        print(f"Imported files: {report['files']} | unchanged skipped: {report['skipped_unchanged']}")
        print(f"Invalid: {len(report['invalid'])} | date/content conflicts: {len(report['conflicts'])} | "
              f"not imported: {len(report['not_imported'])}")
        print(f"v2_observations: {before.get('v2_observations', 0)} -> {after.get('v2_observations')}")
    return report
