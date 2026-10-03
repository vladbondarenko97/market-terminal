"""Build, save and deliver the rendered report. No collection or analytics happen here.

The plain-text body is exactly the saved daily_market_report.txt. The complete MIME message is written to
email.eml before any SMTP connection, so body and attachments are reproducible and can be resent.
"""
import html
import os
import smtplib
import sqlite3
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate

from config import (DATA_DIR, EMAIL_PASSWORD, EMAIL_SENDER, NTFY_URL, PROJECT_ROOT, RECIPIENT_EMAIL, SMTP_PORT,
                    SMTP_SERVER)

EMAIL_PAIRS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]


def alphaflow_appendix():
    """Legacy AlphaFlow section, passed through unchanged and read-only (AlphaFlow is outside v2 scope)."""
    text = ""
    try:
        alpha_db = str(PROJECT_ROOT / "alphaflow" / "alphaflow.db")
        if os.path.exists(alpha_db):
            conn = sqlite3.connect(f"file:{alpha_db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM swing_plays ORDER BY spend DESC LIMIT 10")
            plays = c.fetchall()
            conn.close()
            if plays:
                text += "\n\n🔥 ALPHAFLOW: SMART MONEY SMALL-CAP SWEEPS\n============================================\n"
                for p in plays:
                    text += f"[{p['time_of_sweep']}] {p['contract']} | Spend: ${p['spend']:,.2f} | Vol: {p['volume']} (OI: {p['oi']}) | Rec. Expiry: {p['expiration']} | Risk: ~${p['max_risk']}\n"
    except Exception as e:
        text += f"\n\n[AlphaFlow Error: {e}]"
    return text


def build_email(ctx, report_text, manifest, daily_dir):
    run = ctx["run"]
    msg = EmailMessage()
    msg["Subject"] = f"📈 Daily Market Report & Options Brief | {run['run_folder']}"
    msg["From"] = EMAIL_SENDER or "portfolio-dashboard@localhost"
    msg["To"] = RECIPIENT_EMAIL
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = f"<{run['run_id']}@portfolio-dashboard.local>"
    msg["X-Run-ID"] = run["run_id"]
    msg.set_content(report_text)

    body = [f"""<html><head><style>
      body {{ font-family: monospace; color: #333; }}
      pre {{ font-family: monospace; white-space: pre-wrap; }}
      table {{ width: 100%; max-width: 1400px; margin-bottom: 30px; border-collapse: collapse; }}
      td {{ width: 50%; padding: 10px; vertical-align: top; text-align: center; }}
      img {{ max-width: 100%; height: auto; border: 1px solid #ccc; border-radius: 4px; }}
      h3 {{ font-family: sans-serif; border-bottom: 2px solid #ddd; padding-bottom: 5px; }}
      .na {{ color: #888; }}
    </style></head><body><pre>{html.escape(report_text)}</pre><hr><h2>Market Charts</h2>"""]
    attach = []
    for fam in EMAIL_PAIRS:
        entries = [c for c in manifest if c["family"] == fam]
        if not entries:
            continue
        body.append(f"<h3>{html.escape(entries[0]['email_title'])}</h3><table><tr>")
        for c in entries:
            label = html.escape(c["window_label"])
            if c["status"] in ("generated", "stale_input") and c.get("path") and os.path.exists(c["path"]):
                cid = f"{c['file'].replace('.png', '')}.{run['run_id']}@portfolio-dashboard"
                warn = (f"<br><span style='color:#b25e00'>⚠ {html.escape(c['reason'])}</span>"
                        if c["status"] == "stale_input" else "")
                body.append(f"<td><strong>{label}</strong>{warn}<br><img src='cid:{cid}'></td>")
                attach.append((c["path"], cid))
            else:
                body.append(f"<td class='na'><strong>{label}</strong><br><em>Not available: "
                            f"{html.escape(c.get('reason') or c['status'])}</em></td>")
        body.append("</tr></table>")
    body.append("</body></html>")
    msg.add_alternative("".join(body), subtype="html")
    html_part = msg.get_payload()[1]
    for path, cid in attach:
        with open(path, "rb") as f:
            html_part.add_related(f.read(), maintype="image", subtype="png", cid=f"<{cid}>",
                                  filename=os.path.basename(path))
    return msg


def save_eml(msg, path):
    data = msg.as_bytes(policy=policy.SMTP)
    tmp = str(path) + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return data


def load_eml(path):
    with open(path, "rb") as f:
        return BytesParser(policy=policy.default).parse(f)


def plain_body(msg):
    part = msg.get_body(preferencelist=("plain",))
    return part.get_content() if part else None


def deliver(eml_path):
    """Send a saved message. Returns (status, detail): smtp_accepted | failed | outcome_unknown.
    SMTP acceptance is not proof of inbox delivery."""
    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        return "failed", "EMAIL_SENDER/EMAIL_PASSWORD not configured"
    if not SMTP_SERVER or not RECIPIENT_EMAIL:
        return "failed", "SMTP_SERVER/RECIPIENT_EMAIL not configured"
    msg = load_eml(eml_path)
    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=60)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
    except Exception as e:
        return "failed", f"connect/login: {e}"
    try:
        server.send_message(msg)
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as e:
        return "failed", f"rejected: {e}"
    except Exception as e:
        return "outcome_unknown", f"connection lost during send ({e}); not retried automatically"
    try:
        server.quit()
    except Exception:
        pass
    return "smtp_accepted", f"accepted by {SMTP_SERVER} for {RECIPIENT_EMAIL}"


def _hdr(value):
    """HTTP headers are ASCII-only: encode emoji / non-ASCII as RFC 2047 (ntfy decodes it); newlines as literal \\n."""
    import base64
    value = str(value).replace("\r", "").replace("\n", "\\n")
    return value if value.isascii() else "=?UTF-8?B?" + base64.b64encode(value.encode()).decode() + "?="


def ntfy_push(items):
    """Short event alerts. items: [(title, text, priority, tags)]. Returns per-item outcome."""
    import requests
    from core import lake
    out = []
    if not NTFY_URL:
        return [{"title": t[0], "status": "skipped", "error": "NTFY_URL not configured"} for t in items]
    for title, text, priority, tags in items:
        try:
            r = requests.post(NTFY_URL, data=text.encode("utf-8"), timeout=20,
                              headers={"Title": _hdr(title), "Priority": str(priority), "Tags": tags})
            r.raise_for_status()
            out.append({"title": title, "status": "sent"})
        except Exception as e:
            out.append({"title": title, "status": "failed", "error": lake.redact(str(e))[:200]})
    return out


def ntfy_brief(ctx, report_text, dashboard_url=None, report_url=None):
    """The one daily push: ranked summary on the lock screen + the full report (same text as the email) as a dated
    .txt attachment + a Dashboard button. ntfy.sh keeps attachments for 3 hours; the email keeps the permanent copy."""
    import requests
    from core import lake, render
    from upload_data import report_filename
    try:
        title, message, prio, tags = render.ntfy_summary(ctx)
    except Exception as e:
        return {"status": "failed", "error": lake.redact(f"summary: {e}")[:200]}
    fname = report_filename(ctx)
    headers = {"Title": _hdr(title), "Message": _hdr(message), "Priority": str(prio), "Tags": ",".join(tags),
               "Filename": fname}
    actions = []
    if report_url:
        actions.append(f"view, Full report, {report_url}")
    if dashboard_url:
        actions.append(f"view, Dashboard, {dashboard_url}")
    if actions:
        headers["Actions"] = "; ".join(actions)
    try:
        r = requests.put(NTFY_URL, data=report_text.encode("utf-8"), headers=headers, timeout=30)
        err = None if r.ok else f"HTTP {r.status_code}"
    except requests.ReadTimeout as e:
        # ntfy may already have delivered it; a fallback push could duplicate the brief
        return {"title": title, "status": "outcome_unknown", "error": lake.redact(str(e))[:200], "priority": prio}
    except requests.RequestException as e:
        err = str(e)
    if err is None:
        try:
            att = r.json().get("attachment") or {}
        except ValueError:
            att = {}
        return {"title": title, "status": "sent", "priority": prio, "attachment": att.get("name"),
                "attachment_expires": att.get("expires"), "summary_bytes": len(message.encode())}
    # attachment upload refused or unreachable: still deliver the summary as a plain message
    fallback = ntfy_push([(title, message.replace("📎 Full report attached (same as the email)", "Full report: see email"),
                           prio, ",".join(tags))])
    return {"title": title, "status": "sent_without_attachment" if fallback[0]["status"] == "sent" else "failed",
            "error": lake.redact(err)[:200], "priority": prio}

def generate_and_send():
    """Legacy entry point: deliver the latest committed run's saved email (no recollection)."""
    from main_pipeline import resend
    return resend(None)


if __name__ == "__main__":
    generate_and_send()
