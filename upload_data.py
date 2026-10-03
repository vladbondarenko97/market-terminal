import os
import sqlite3
import tempfile
import requests
from config import DATA_DIR, DB_PATH, optional_env, run_folder_name

UPLOAD_URL = optional_env("UPLOAD_URL", default="")       # the receiver in server/, e.g. https://example.com/admin/data/upload_receiver.php
UPLOAD_TOKEN = optional_env("UPLOAD_TOKEN", default="")   # kept in .env, not in source control


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


def upload_report(report_path, name):
    """Upload the daily report to the hardened receiver (server/upload_receiver.php). Returns the permanent URL or None.
    Enabled only with REPORT_UPLOAD=1 once the server is locked down (see server/README.md)."""
    if optional_env("REPORT_UPLOAD", "") != "1" or not UPLOAD_TOKEN or not UPLOAD_URL:
        return None
    try:
        with open(report_path, "rb") as fh:
            r = requests.post(UPLOAD_URL, data={'token': UPLOAD_TOKEN}, files={'report': (name, fh)}, timeout=60)
        return (r.json().get("report") or {}).get("url") if r.status_code == 200 else None
    except Exception:
        return None


def upload_files(daily_dir=None):
    """Returns (status, detail)."""
    print("\n--- UPLOADING TO SERVER ---")
    daily_dir = daily_dir or os.path.join(DATA_DIR, run_folder_name())
    files, handles, slim = {}, [], None
    try:
        if os.path.exists(DB_PATH):
            slim = slim_db_copy()
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
        if not UPLOAD_TOKEN or not UPLOAD_URL:
            return "failed", "UPLOAD_URL/UPLOAD_TOKEN not configured in .env"
        response = requests.post(UPLOAD_URL, data={'token': UPLOAD_TOKEN}, files=files, timeout=60)
        if response.status_code == 200:
            print("✅ Upload successful!")
            return "uploaded", response.text.strip()[:300]
        return "failed", f"HTTP {response.status_code}: {response.text.strip()[:300]}"
    except Exception as e:
        return "failed", str(e)
    finally:
        for h in handles:
            h.close()
        if slim and os.path.exists(slim):
            os.remove(slim)


if __name__ == "__main__":
    print(upload_files())
