"""Recover historical engine positions from sent report emails (read-only on the mail source).

  python scripts/import_email_positions.py                 # Apple Mail store (messages from REPORT_SENDER)
  python scripts/import_email_positions.py --dir ~/Downloads/reports   # exported .eml / .emlx / .mbox files

Idempotent: each email is keyed by its Message-ID.
"""
import argparse
import email
import json
import mailbox
import os
import re
import sys
from email import policy
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import DB_PATH, REPORT_SENDER  # noqa: E402
from core import lake, positions  # noqa: E402

SENDER = REPORT_SENDER      # the address the reports are sent from (.env: REPORT_SENDER, else EMAIL_SENDER)


def _grab(text, key):
    i = text.find(f'"{key}"')
    if i < 0:
        return None
    j = text.find(":", i) + 1
    while j < len(text) and text[j] in " \n\r\t":
        j += 1
    if text[j] == '"':
        return text[j + 1:text.find('"', j + 1)]
    if text[j] != "{":
        return text[j:text.find("\n", j)].strip().rstrip(",")
    depth = 0
    for k in range(j, len(text)):
        depth += {"{": 1, "}": -1}.get(text[k], 0)
        if depth == 0:
            try:
                return json.loads(text[j:k + 1])
            except ValueError:
                return None
    return None


def _messages_from_apple_mail(stats):
    """Use Mail's own index to find report emails from SENDER, then open only those .emlx files (read-only)."""
    import shutil
    import sqlite3
    import tempfile
    base = Path.home() / "Library" / "Mail"
    versions = [p for p in base.glob("V*") if p.name[1:].isdigit()]
    if not versions:
        raise SystemExit(f"No Apple Mail data folder (V<n>) under {base}; pass --dir with exported messages")
    vdir = max(versions, key=lambda p: int(p.name[1:]))   # V10 > V9
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for suffix in ("", "-wal", "-shm"):
            src = vdir / "MailData" / f"Envelope Index{suffix}"
            if src.exists():
                shutil.copy(src, tmp / f"idx.db{suffix}")
        c = sqlite3.connect(tmp / "idx.db")
        try:
            ids = {str(r[0]) for r in c.execute(
                """SELECT m.ROWID FROM messages m JOIN subjects s ON s.ROWID = m.subject
                   JOIN addresses a ON a.ROWID = m.sender WHERE a.address = ? AND s.subject LIKE '%Market Report%'""",
                (SENDER,))}
        finally:
            c.close()
    stats["indexed"] = len(ids)
    found = set()
    for dirpath, _, files in os.walk(vdir):
        for f in files:
            if f.endswith(".emlx") and f.split(".")[0] in ids:
                rid = f.split(".")[0]
                if rid in found:
                    continue
                found.add(rid)
                raw = open(os.path.join(dirpath, f), "rb").read()
                yield email.message_from_bytes(raw[raw.find(b"\n") + 1:], policy=policy.default)
    stats["bodies_on_disk"] = len(found)
    stats["headers_only"] = len(ids - found)


def _messages_from_dir(d):
    for p in Path(d).expanduser().rglob("*"):
        if p.suffix == ".mbox" or p.name == "mbox":
            for m in mailbox.mbox(str(p)):
                yield email.message_from_bytes(m.as_bytes(), policy=policy.default)
        elif p.suffix in (".eml", ".emlx"):
            raw = p.read_bytes()
            if p.suffix == ".emlx":
                raw = raw[raw.find(b"\n") + 1:]
            yield email.message_from_bytes(raw, policy=policy.default)


def main(argv=None):
    if not SENDER:
        sys.exit("Set REPORT_SENDER (or EMAIL_SENDER) in .env: the address the report emails were sent from.")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", help="folder of exported .eml/.emlx/.mbox files (default: Apple Mail)")
    a = ap.parse_args(argv)
    conn = lake.connect(DB_PATH)
    lake.migrate(conn)
    stats = {"report_emails": 0, "positions": 0, "cash": 0, "new": 0, "no_ticket": 0}
    months = {}
    span = []
    for msg in (_messages_from_dir(a.dir) if a.dir else _messages_from_apple_mail(stats)):
        if "Market Report" not in str(msg["Subject"] or "") or SENDER not in str(msg["From"] or ""):
            continue
        run_id = str(msg["X-Run-ID"] or "")
        if run_id and conn.execute("SELECT 1 FROM v2_trade_signals WHERE run_id = ?", (run_id,)).fetchone():
            stats["already_recorded"] = stats.get("already_recorded", 0) + 1   # v2 run: recorded under its run_id
            continue
        part = msg.get_body(preferencelist=("plain",))
        text = part.get_content() if part else ""
        stats["report_emails"] += 1
        sent = parsedate_to_datetime(msg["Date"]).isoformat()
        span.append(sent)
        months[sent[:7]] = months.get(sent[:7], 0) + 1
        price = re.search(r'"current_price":\s*([\d.]+)', text) or re.search(r'"spot":\s*([\d.]+)', text)
        sig = positions.signal_from_email(sent, str(msg["Message-ID"] or sent), _grab(text, "live_trade_ticket"),
                                          _grab(text, "total_score"), _grab(text, "directional_bias"),
                                          float(price.group(1)) if price else None)
        if not sig:
            stats["no_ticket"] += 1
            continue
        stats["positions"] += 1
        stats["cash"] += sig["position_type"] == "CASH"
        stats["new"] += positions.insert_signal(conn, sig)
    conn.close()
    print(f"{stats} | emails {min(span)[:10] if span else '-'} → {max(span)[:10] if span else '-'} | by month {dict(sorted(months.items()))}")
    return stats


if __name__ == "__main__":
    main()
