import os
import sqlite3
import sys
import tempfile
import requests
from config import DATA_DIR, DB_PATH, REPORT_UPLOAD, UPLOAD_TOKEN, UPLOAD_URL, run_folder_name
from core import lake

# UPLOAD_URL is the receiver in server/, e.g. https://example.com/admin/data/upload_receiver.php.
# UPLOAD_TOKEN stays in .env, not in source control. REPORT_UPLOAD=1 also uploads the daily report.


def slim_db_copy(db_path=DB_PATH):
    """Consistent copy of portfolio.db without the local v2 data-lake tables, so the server receives the same
    legacy tables it always did (and not the large raw-payload archive). The original DB is not modified."""
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="portfolio_upload_")
    os.close(fd)
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)
    with dst:
        src.backup(dst)
    src.close()
    objs = dst.execute("SELECT type, name FROM sqlite_master WHERE name LIKE 'v2_%' AND type IN ('view','trigger','table')").fetchall()
    for kind, name in sorted(objs, key=lambda o: {"view": 0, "trigger": 1, "table": 2}[o[0]]):
        dst.execute(f'DROP {kind.upper()} IF EXISTS "{name}"')   # temporary copy only
    dst.commit()
    dst.execute("VACUUM")
    dst.close()
    return tmp


def report_filename(ctx):
    """Dated report name; server/upload_receiver.php only accepts ^market-report-YYYY-MM-DD-HHMMZ.txt$."""
    t = ctx["run"]["generated_at"]
    return f"market-report-{t[:10]}-{t[11:13]}{t[14:16]}Z.txt"


def _unconfigured():
    """None when UPLOAD_URL and UPLOAD_TOKEN are both set; otherwise the (status, detail) to report.
    Neither set = `skipped` (upload is not used); only one set = `failed` (a half-finished setup)."""
    if UPLOAD_URL and UPLOAD_TOKEN:
        return None
    if not UPLOAD_URL and not UPLOAD_TOKEN:
        return "skipped", "UPLOAD_URL and UPLOAD_TOKEN are not set (upload not configured)"
    return "failed", "UPLOAD_URL and UPLOAD_TOKEN must both be set in .env (one of them is empty)"


def check_reply(response, expected):
    """Judge the receiver's answer (server/upload_receiver.php). It replies HTTP 200 with a JSON object
    {"<field>": {"status": "ok" | "rejected" | "error", ...}} for every file it was sent, so a 200 alone proves nothing.
    Returns (ok, detail, reply_json): ok only when every field in `expected` came back with status "ok"."""
    if response.status_code != 200:
        return False, lake.redact(f"HTTP {response.status_code}: {response.text.strip()[:300]}"), None
    try:
        reply = response.json()
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        return False, lake.redact(f"unexpected reply (not a JSON object): {response.text.strip()[:200]}"), None
    parts, ok = [], True
    for key in expected:
        entry = reply.get(key)
        state = entry.get("status") if isinstance(entry, dict) else None
        parts.append(f"{key}: {state or 'missing from the reply'}")
        ok = ok and state == "ok"
    return ok, "; ".join(parts), reply


def upload_report_result(report_path, name):
    """Upload the daily report to the hardened receiver (server/upload_receiver.php).
    Returns {"status": "skipped" | "uploaded" | "failed", "detail": ..., "url": permanent URL when uploaded}.
    Enabled only with REPORT_UPLOAD=1 once the server is locked down (see server/README.md); without it the result is
    `skipped`. With REPORT_UPLOAD=1 but no receiver address or token it is `failed`."""
    if not REPORT_UPLOAD:
        return {"status": "skipped", "detail": "REPORT_UPLOAD is not 1"}
    problem = _unconfigured()
    if problem:
        return {"status": "failed", "detail": f"REPORT_UPLOAD=1 but {problem[1]}"}
    try:
        with open(report_path, "rb") as fh:
            r = requests.post(UPLOAD_URL, data={'token': UPLOAD_TOKEN}, files={'report': (name, fh)}, timeout=60)
        ok, detail, reply = check_reply(r, ["report"])
        url = ((reply or {}).get("report") or {}).get("url") if ok else None
        if ok and not url:
            return {"status": "failed", "detail": "the receiver accepted the report but returned no URL"}
        return {"status": "uploaded", "detail": detail, "url": url} if ok else {"status": "failed", "detail": detail}
    except Exception as e:
        return {"status": "failed", "detail": lake.redact(f"{type(e).__name__}: {e}")[:300]}


def upload_report(report_path, name):
    """The permanent URL of the uploaded report, or None (not enabled, not configured or failed)."""
    return upload_report_result(report_path, name).get("url")


def upload_files(daily_dir=None):
    """Upload the legacy database copy and the dashboard XML. Returns (status, detail):
    uploaded (the receiver answered `ok` for every file) | skipped (not configured) | failed."""
    problem = _unconfigured()
    if problem:                      # checked first: no point building a database copy nobody will receive
        return problem
    print("\n--- UPLOADING TO SERVER ---")
    daily_dir = daily_dir or os.path.join(DATA_DIR, run_folder_name())
    files, handles, slim = {}, [], None
    try:
        if os.path.exists(DB_PATH):
            slim = slim_db_copy(DB_PATH)
            h = open(slim, 'rb'); handles.append(h)
            files['portfolio'] = ('portfolio.db', h)
            print(f"📦 Prepared portfolio.db (legacy tables, {os.path.getsize(slim) / 1e6:.1f} MB)")
        dashboard_xml = os.path.join(daily_dir, "volume_dashboard.xml")
        dashboard_txt = os.path.join(daily_dir, "volume_dashboard.txt")
        path = dashboard_xml if os.path.exists(dashboard_xml) else dashboard_txt if os.path.exists(dashboard_txt) else None
        if path:
            h = open(path, 'rb'); handles.append(h)
            files['dashboard'] = ('volume_dashboard.xml', h)
        if not files:
            return "failed", "no files to upload"
        response = requests.post(UPLOAD_URL, data={'token': UPLOAD_TOKEN}, files=files, timeout=60)
        ok, detail, _ = check_reply(response, list(files))
        if ok:
            print("✅ Upload successful!")
            return "uploaded", detail
        print(f"⚠️  Upload not accepted: {detail}")
        return "failed", detail
    except Exception as e:
        return "failed", lake.redact(f"{type(e).__name__}: {e}")[:300]
    finally:
        for h in handles:
            h.close()
        if slim and os.path.exists(slim):
            os.remove(slim)


if __name__ == "__main__":
    result = upload_files()
    print(result)
    sys.exit(0 if result[0] == "uploaded" else 2)
