"""v2 run coordinator: acquire -> capture -> calculate -> commit snapshot -> export -> deliver.

  python main_pipeline.py run [--offline] [--no-deliver] [--no-upload] [--trigger scheduled|manual] [--cme-max-files N]
  python main_pipeline.py cme-login                              # one-time CME login (MFA) for the volume FTP
  python main_pipeline.py replay [--run RUN_ID] [--out DIR]     # offline re-render of a saved run, no delivery
  python main_pipeline.py import-history                         # idempotent import of old files/tables
  python main_pipeline.py resend [--run RUN_ID]                  # explicit resend of a saved email.eml
  python main_pipeline.py catalog [--run RUN_ID]                 # variable catalog + lineage (Markdown)
  python main_pipeline.py status
"""
import argparse
import csv
import fcntl
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import config
from config import DATA_DIR, DB_PATH, PROJECT_ROOT, run_folder_name
from core import catalog, lake, render

LOCK_PATH = DATA_DIR / ".v2_run.lock"
STATUS_PATH = DATA_DIR / ".v2_run_status.json"
EXIT_BUSY = 75


# ============================================================ helpers
def code_version():
    try:
        sha = subprocess.check_output(["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"],
                                      text=True, stderr=subprocess.DEVNULL).strip()
        dirty = subprocess.call(["git", "-C", str(PROJECT_ROOT), "diff", "--quiet", "HEAD"],
                                stderr=subprocess.DEVNULL) != 0
        return sha + ("-dirty" if dirty else "")
    except Exception:
        return "unknown"


def write_atomic(path, data):
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp")
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(tmp, mode, **({} if mode == "wb" else {"encoding": "utf-8"})) as f:
        f.write(data)
    os.replace(tmp, path)


def set_status(**kw):
    try:
        cur = json.loads(STATUS_PATH.read_text()) if STATUS_PATH.exists() else {}
    except ValueError:
        cur = {}
    cur.update(kw, updated_at=lake.utc_now_iso())
    write_atomic(STATUS_PATH, json.dumps(cur, indent=2))


class RunLock:
    """One run owns collection and exports. A second request gets a clear busy answer."""
    def __init__(self):
        self.fh = None

    def acquire(self):
        self.fh = open(LOCK_PATH, "a+")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fh.close()
            self.fh = None
            return False
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return True

    def release(self):
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
            self.fh = None


def _csv_append(path, header_fallback, row):
    """Append to a legacy CSV keeping its existing header layout (columns it doesn't know are dropped
    from the CSV only; the SQLite copy keeps them)."""
    path = Path(path)
    exists = path.exists() and path.stat().st_size > 0
    if exists:
        with open(path, newline="", encoding="utf-8-sig") as f:
            header = next(csv.reader(f))
    else:
        header = header_fallback
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(header)
        w.writerow(["NaN" if row.get(h) is None and "macro" in path.name else
                    ("N/A" if row.get(h) is None else row.get(h)) for h in header])


# ============================================================ exports
def export_ledgers(conn, ctx, run_id, stages):
    """Legacy CSV + SQLite ledger rows. Each step is recorded so a retry never appends twice."""
    done = stages.setdefault("ledgers", {})
    rows = render.ledger_rows(ctx)
    for table, recs in rows.items():
        if done.get(table) == "done":
            continue
        try:
            if table == "crypto_metrics_history":
                have = {r[0] for r in conn.execute("SELECT Date FROM crypto_metrics_history")} \
                    if lake.table_columns(conn, table) else set()
                recs = [r for r in recs if r["Date"] not in have]
            for r in recs:
                lake.append_legacy_row(conn, table, r)
                if table != "crypto_metrics_history":
                    _csv_append(DATA_DIR / f"{table}.csv", list(r.keys()), r)
            done[table] = "done"
        except Exception as e:
            done[table] = f"error: {e}"
    # COMEX inventory: only a newly acquired report, keyed by its report date
    inv = ctx["inventory"]
    acq = inv.get("acquisition") or {}
    if done.get("comex_inventory_history") != "done":
        if acq.get("outcome") == "ok" and inv.get("registered") is not None:
            row = {"Date": inv["report_date"], "Registered": inv["registered"], "Eligible": inv["eligible"],
                   "Total": inv["total"], "Reg_Change": inv["reg_change"], "Elig_Change": inv["elig_change"],
                   "Total_Change": inv["total_change"]}
            existing = {(round(r[0] or 0), round(r[1] or 0)) for r in
                        conn.execute("SELECT Registered, Eligible FROM comex_inventory_history")} \
                if lake.table_columns(conn, "comex_inventory_history") else set()
            if (round(row["Registered"]), round(row["Eligible"])) in existing:
                done["comex_inventory_history"] = "done"   # this report is already recorded
            else:
                try:
                    lake.append_legacy_row(conn, "comex_inventory_history", row)
                    _csv_append(DATA_DIR / "comex_inventory_history.csv", list(row.keys()), row)
                    done["comex_inventory_history"] = "done"
                except Exception as e:
                    done["comex_inventory_history"] = f"error: {e}"
        else:
            done["comex_inventory_history"] = f"skipped: no new report ({acq.get('outcome')})"
    return rows


def flow_history(conn, as_of):
    from core import history
    df = history.flow_ledger_daily(conn, "SLV", as_of)
    out = []
    for _, r in df.tail(5).iterrows():
        out.append({"date": r["_ts"].strftime("%Y-%m-%d"), "sentiment": r.get("DP_Sentiment"),
                    "vwap": r.get("DP_VWAP")})
    return out


def render_outputs(conn, ctx, out_dir, *, rows=None):
    """Render every file for one context. Pure: no provider calls. Returns (artifacts, report_text, manifest)."""
    import visualize_volume
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = visualize_volume.render_charts(conn, ctx, str(out_dir))
    rows = rows or render.ledger_rows(ctx)
    dp = {r["Ticker"]: r for r in rows.get("equities_darkpool_gex_ledger", [])}
    tactical = render.tactical_xml(ctx)
    report = render.full_report(ctx, manifest, flow_history=flow_history(conn, ctx["run"]["generated_at"]),
                                spy_row=dp.get("SPY"), slv_row=dp.get("SLV"),
                                appendix=(ctx.get("appendix") or {}).get("alphaflow_text", ""))
    files = {
        "tactical_ruling.txt": tactical,
        "volume_dashboard.txt": render.dashboard_xml(ctx, tactical),
        "master_market_data.csv": render.master_market_csv(ctx),
        "daily_market_report.txt": report,
        "report_snapshot.json": lake.dumps(ctx, indent=2),
    }
    template = (PROJECT_ROOT / "volume_dashboard.html").read_text(encoding="utf-8")
    files["volume_dashboard.html"] = render.dashboard_html(ctx, template, tactical, manifest, report_text=report)
    for name, data in files.items():
        write_atomic(out_dir / name, data)
    from send_email import build_email, save_eml
    msg = build_email(ctx, report, manifest, str(out_dir))
    eml = save_eml(msg, out_dir / "email.eml")
    artifacts = {name: {"sha256": lake.sha256_bytes(d.encode() if isinstance(d, str) else d)} for name, d in files.items()}
    artifacts["email.eml"] = {"sha256": lake.sha256_bytes(eml), "bytes": len(eml)}
    manifest_doc = {"run_id": ctx["run"]["run_id"], "out_dir": str(out_dir), "artifacts": artifacts,
                    "charts": [{k: v for k, v in c.items() if k != "trace"} for c in manifest]}
    write_atomic(out_dir / "run_manifest.json", json.dumps(manifest_doc, indent=2, default=str))
    return artifacts, report, manifest, eml


# ============================================================ run
def run(*, offline=False, deliver=True, upload=True, trigger="manual", use_browser=True, skip_cme=False,
        max_volume_files=10, login_wait_seconds=900):
    from core import collect
    from core.sources import SourceSession
    from send_email import alphaflow_appendix, deliver as smtp_deliver, ntfy_brief, ntfy_push
    if trigger == "scheduled":
        from core.market_calendar import scheduled_run_skip_reason
        skip = ("scheduled runs are off on this machine (SCHEDULED_RUNS=1 is not set in .env)"
                if not config.SCHEDULED_RUNS else scheduled_run_skip_reason())
        if skip:
            print(f"⏭️  Scheduled run skipped: {skip}")
            return 0
    config.ensure_data_dir()
    lock = RunLock()
    if not lock.acquire():
        try:
            cur = json.loads(STATUS_PATH.read_text())
        except Exception:
            cur = {}
        print(f"⏳ BUSY: run {cur.get('run_id')} is in progress (stage {cur.get('stage')}). Not starting another.")
        return EXIT_BUSY
    t0 = time.monotonic()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    run_id = f"{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
    folder = run_folder_name(now)
    mode = "offline" if offline else "live"
    conn = lake.connect(DB_PATH)
    lake.migrate(conn)
    lake.create_run(conn, run_id, mode=mode, trigger=trigger, run_folder=folder, code_version=code_version())
    stages = {}
    set_status(run_id=run_id, stage="collecting", state="running", pid=os.getpid(), mode=mode, started_at=now.isoformat())
    print(f"🚀 v2 run {run_id} ({mode}, trigger={trigger}) → {DATA_DIR / folder}")
    try:
        session = SourceSession(conn, run_id, offline=offline)
        run_meta = {"run_id": run_id, "generated_at": now.isoformat(), "run_folder": folder, "mode": mode,
                    "code_version": code_version(), "trigger": trigger}
        notify = (lambda m: ntfy_push([("CME login needed", m, "urgent", "key")])) if deliver else None
        set_status(stage="collecting (a CME login window may be waiting for you)")
        ctx, frames = collect.build_context(conn, session, run_meta, use_browser=use_browser, skip_cme_download=skip_cme,
                                           max_volume_files=max_volume_files, login_wait_seconds=login_wait_seconds,
                                           notify=notify)
        ctx["appendix"] = {"alphaflow_text": alphaflow_appendix()}
        stages["collect"] = {"status": "done", "seconds": round(time.monotonic() - t0, 1), "detail": ctx["stage_seconds"]}
        set_status(stage="capturing")
        n_obs = lake.record_observations(conn, collect.context_observations(ctx, frames), run_id=run_id)
        stages["capture"] = {"status": "done", "new_observations": n_obs}
        lake.commit_snapshot(conn, run_id, ctx)
        ctx = lake.load_snapshot(conn, run_id)     # every renderer consumes the committed snapshot
        stats = session.stats()
        lake.update_run(conn, run_id, status="committed", stages_json=stages, request_counts_json=lake.redact(lake.dumps(stats)),
                        sources_json={k: {"status": v.get("status"), "reason": v.get("reason")}
                                      for k, v in ctx.items() if isinstance(v, dict) and "status" in v})
        print(f"💾 Snapshot committed ({n_obs} new observations, provider calls {stats['calls']}, "
              f"dedup hits {stats['dedup_hits']})")
    except Exception as e:
        import traceback
        lake.update_run(conn, run_id, status="failed", error=lake.redact(traceback.format_exc()[-4000:]), stages_json=stages,
                        elapsed_s=time.monotonic() - t0, finished_at=lake.utc_now_iso())
        set_status(stage="failed", state="failed", error=str(e))
        lock.release()
        print(f"❌ Run failed before the snapshot was committed: {e}")
        raise
    warnings = []
    try:
        set_status(stage="exporting")
        rows = None
        if mode == "live":
            rows = export_ledgers(conn, ctx, run_id, stages)
            from core import positions
            sig = positions.record_signal(conn, ctx)
            stages["trade_signal"] = sig["position_type"] if sig else "none"
            from core import forecast as _fc
            stages["forecasts_logged"] = _fc.record_forecasts(conn, ctx)
            bad = {k: v for k, v in stages["ledgers"].items() if str(v).startswith("error")}
            if bad:
                warnings.append(f"ledger errors: {bad}")
        else:
            stages["ledgers"] = "skipped (offline run does not append legacy ledgers)"
        out_dir = DATA_DIR / folder
        artifacts, report, manifest, eml = render_outputs(conn, ctx, out_dir, rows=rows)
        rpid = lake.store_payload(conn, source="v2_render", kind="daily_market_report", content=report, fmt="text",
                                  run_id=run_id)
        epid = lake.store_payload(conn, source="v2_render", kind="email_mime", content=eml, fmt="eml", run_id=run_id)
        artifacts["daily_market_report.txt"]["payload_id"] = rpid
        artifacts["email.eml"]["payload_id"] = epid
        stages["export"] = {"status": "done", "charts": {c["file"]: c["status"] for c in manifest}}
        lake.update_run(conn, run_id, artifacts_json={"out_dir": str(out_dir), "files": artifacts,
                                                      "charts": [{k: v for k, v in c.items() if k != "trace"}
                                                                 for c in manifest]},
                        stages_json=stages)
        print(f"📁 Exports written to {out_dir}")
        if deliver:
            set_status(stage="delivering")
            lake.update_run(conn, run_id, delivery_status="attempting")
            status, detail = smtp_deliver(out_dir / "email.eml")
            from upload_data import report_filename, upload_report
            report_url = upload_report(out_dir / "daily_market_report.txt", report_filename(ctx)) if upload else None
            pushes = [ntfy_brief(ctx, report, config.optional_env("DASHBOARD_URL", ""), report_url)] + ntfy_push(_refinery_alerts(ctx))
            up = upload_status = None
            if upload:
                from upload_data import upload_files
                upload_status, up = upload_files(str(out_dir))
            lake.update_run(conn, run_id, delivery_status=status,
                            delivery_detail=json.dumps({"smtp": detail, "ntfy": pushes, "upload": [upload_status, up]}))
            print(f"✉️  Email: {status} — {detail}")
            if status != "smtp_accepted":
                warnings.append(f"email {status}")
        else:
            lake.update_run(conn, run_id, delivery_status="not_requested")
    except Exception as e:
        import traceback
        warnings.append(f"export/delivery error: {e}")
        stages["export_error"] = lake.redact(traceback.format_exc()[-3000:])
    elapsed = time.monotonic() - t0
    final = "completed_with_warnings" if warnings else "completed"
    lake.update_run(conn, run_id, status=final, stages_json=stages, finished_at=lake.utc_now_iso(), elapsed_s=elapsed,
                    error=lake.redact("; ".join(warnings)) or None,
                    request_counts_json=lake.redact(lake.dumps(session.stats())))
    set_status(stage="done", state=final, finished_at=lake.utc_now_iso(), elapsed_s=round(elapsed, 1))
    conn.close()
    lock.release()
    print(f"🎉 Run {run_id} {final} in {elapsed:.1f}s" + (f" — {'; '.join(warnings)}" if warnings else ""))
    return 0


def _refinery_alerts(ctx):
    """One push per newly seen major unplanned outage (large refinery, key unit, or 24h+)."""
    items = []
    for e in ((ctx.get("refining") or {}).get("outages") or {}).get("new_major_this_run") or []:
        ref = e.get("refinery") or {}
        body = (f"{ref.get('company')} {ref.get('site')} ({(ref.get('capacity_bpd') or 0) / 1000:.0f}k bpd)\n"
                f"{e.get('event_type')} · {', '.join(e.get('units_classified') or []) or 'unit n/a'}\n"
                f"Start {e.get('start')} · duration {e.get('duration') or 'ongoing/unknown'}\n{(e.get('cause') or '')[:300]}\n{e.get('url')}")
        items.append((f"⚠ Refinery outage: {ref.get('site')}", body, "urgent", "oil_drum,warning"))
    return items


# ============================================================ replay / resend / catalog
SIGNAL_STATE = DATA_DIR / "signal_alert_state.json"


def _live_quotes():
    import yfinance as yf
    from core import watch
    out = {}
    for key, sym in [("SPY", "SPY"), ("SLV", "SLV"), ("SI_F", "SI=F")] + [(s, s) for s in watch.watchlist() if s not in ("SPY", "SLV")]:
        try:
            out[key] = float(yf.Ticker(sym).fast_info["last_price"])
        except Exception:
            pass                                    # no quote: that signal stays as of the last run
    return out


def signal_alerts(dry_run=False, test=False):
    """Phone (ntfy) + Mac notification for every Signal Watch row that went from waiting to FIRED since the last check,
    and for live-watch exits. Price-driven rows are re-evaluated on live quotes; the others change when a pipeline run
    commits. One alert per signal per day, so a price sitting on its trigger cannot flap. The first check only records
    the state. A fired dip rule also opens a paper position (core/watch.py) that the exit alert later closes."""
    import yfinance as yf
    from datetime import date
    from core import forecast as F, watch as W
    from core.market_calendar import scheduled_run_skip_reason
    from send_email import ntfy_push
    skip = None if dry_run or test else scheduled_run_skip_reason(early_minutes=0)
    if skip:
        return 0
    conn = lake.connect(DB_PATH)
    try:
        lake.migrate(conn)
        ctx = lake.load_snapshot(conn, _latest_run_id(conn))
        ticket = conn.execute("SELECT * FROM v2_trade_signals WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 1").fetchone()
        if not ctx or not ctx.get("forecast"):
            print("signal-alerts: no committed run with Forecast Lab data")
            return 1
        quotes = _live_quotes()
        spy = quotes.get("SPY")
        rows = F.signal_watch(F.live_overlay(ctx["forecast"], quotes), dict(ticket) if ticket else None)
        today, now = datetime.now().strftime("%Y-%m-%d"), datetime.now().strftime("%-I:%M %p")
        tail = (f"Engine: {ticket['bias']}\n" if ticket else "") + \
               f"Live quotes {now} ({', '.join(f'{k} {v:.2f}' for k, v in list(quotes.items())[:3]) or 'none'}); " \
               f"other inputs from the {ctx['run'].get('generated_local', 'latest')} run."
        calls = lambda exp: yf.Ticker("SPY").option_chain(exp).calls
        held, items, dip, spread, exp, others = W.open_positions(conn), [], None, None, None, []
        try:                                        # live watch: dip entry (with the exact spread) and due exits
            dip = W.dip_signal(W.fetch_closes(), spy, W.in_entry_window(), bool(held))
            if dip and dip["fired"]:
                exp = W.pick_expiry(yf.Ticker("SPY").options)
                spread = W.pick_spread(calls(exp), spy) if exp else None
                if spread:
                    dip["action"] = (f"Buy SPY {exp} {spread['long_strike']:g}/{spread['short_strike']:g} call spread near "
                                     f"{spread['entry_debit']:.2f} debit (max loss ${spread['entry_debit'] * 100:.0f} each); "
                                     f"sell on {W.exit_date(date.today()):%a %b %-d} · {dip['action'].rsplit(' · ', 1)[-1]}")
            for sym in W.watchlist()[1:]:                            # alert only: no paper position for share trades
                row = W.dip_signal(W.fetch_closes(sym), quotes.get(sym), W.in_entry_window(), False, sym, W.fetch_blackout(sym))
                if not row or row["edge"] == "none":
                    continue                                         # no measured edge on this ticker: shown on the card, never alerted
                if row["fired"]:
                    row["action"] += f" (sell on {W.exit_date(date.today()):%a %b %-d})"
                others.append(row)
            for pos in held:
                due = date.fromisoformat(pos["exit_due"])
                if date.today() > due or (date.today() == due and W.in_entry_window()):
                    value = W.spread_value(calls(pos["expiration"]), pos["long_contract"], pos["short_contract"])
                    if value is None:
                        continue
                    pnl = (value / pos["entry_debit"] - 1) * 100
                    items.append((f"SPY dip spread: SELL now ({pnl:+.0f}%)",
                                  f"Sell the SPY {pos['expiration']} {pos['long_strike']:g}/{pos['short_strike']:g} call spread near {value:.2f} "
                                  f"(entered {pos['opened_on']} at {pos['entry_debit']:.2f}).\n\nThe {W.HOLD_DAYS}-trading-day hold is up; "
                                  f"the rule is only tested with this exit.\n{tail}", "high", "moneybag"))
                    if not dry_run and not test:
                        W.close_position(conn, pos["position_id"], value, spy)
        except Exception as e:
            print(f"{today} {now} signal-alerts: live watch unavailable: {lake.redact(str(e))[:200]}")
        rows += [r for r in [dip] + others if r]
        if W.in_entry_window() or dry_run:          # edge lab: once a day is enough, these read daily closes
            try:
                from core import edges as E
                scanned = set(W.watchlist())
                for a in E.payload()["tracked"]:
                    rows += [{"asset": a["symbol"], "name": e["name"], "fired": e["active"] and e["bias"] == "bullish", "bias": "bullish",
                              "action": e["action"], "now": e["now"], "trigger": e["trigger"], "evidence": e["evidence"], "what": e["what"],
                              "where": f"Edge lab card (core/edges.py) · {e['source']}"}
                             for e in a["edges"] if e["edge"] in ("tested", "thin") and e["key"] not in ("volatility", "overnight")
                             and not (e["key"] == "reversal" and a["symbol"] in scanned)]       # the scanner already alerts those
            except Exception as e:
                print(f"{today} {now} signal-alerts: edge lab unavailable: {lake.redact(str(e))[:200]}")
        state = json.loads(SIGNAL_STATE.read_text()) if SIGNAL_STATE.exists() else None
        new_state = dict(state or {})               # rows not evaluated this check (edge lab outside its window) keep their state
        for i, r in enumerate(rows):
            key = f"{r['asset']}|{r['name']}"
            prev = (state or {}).get(key)
            fire = (test and i == 0) or bool(r["fired"] and prev and not prev["fired"] and prev.get("alerted") != today)
            new_state[key] = {"fired": r["fired"], "alerted": today if fire else (prev or {}).get("alerted")}
            if fire:
                arrow = {"bullish": " ▲", "bearish": " ▼"}.get(r["bias"], "")
                items.append((f"{'TEST · ' if test else ''}{r['asset']} {r['name']} FIRED{arrow}",
                              f"{r['action']}\n\nNow: {r['now']}\nTrigger: {r['trigger']}\nEvidence: {r['evidence']}\n\n{r['what']}\n"
                              f"Source: {r['where']}\n{tail}",
                              "high" if r["action"].startswith("Buy") else "default",
                              "chart_with_upwards_trend" if r["bias"] == "bullish" else "chart_with_downwards_trend" if r["bias"] == "bearish" else "warning"))
                if r is dip and spread and not dry_run and not test:
                    W.open_position(conn, spread, exp, spy)
        if dry_run:
            for r in rows:
                print(f"{'FIRED  ' if r['fired'] else 'waiting'} {r['asset']:4} {r['name']:30} {r['now']}")
            print(f"open watch positions: {len(held)}")
            print(f"would alert: {[t[0] for t in items] or 'nothing'}" + ("" if state is not None else " (first check: state would be recorded, no alerts)"))
            return 0
    finally:
        conn.close()
    if items:
        print(f"{today} {now} signal-alerts: {ntfy_push(items)}")
        if sys.platform == "darwin":              # this Mac too: native banner, no ntfy client needed
            for title, text, _, _ in items:
                q = lambda s: json.dumps(s, ensure_ascii=False)
                subprocess.run(["osascript", "-e", f'display notification {q(text.splitlines()[0])} with title {q(title)} sound name "Glass"'],
                               check=False, timeout=15)
    if not test:
        tmp = SIGNAL_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(new_state, indent=1))
        tmp.replace(SIGNAL_STATE)
    return 0


def _latest_run_id(conn):
    row = conn.execute("SELECT run_id FROM v2_latest_snapshot").fetchone()
    return row["run_id"] if row else None


def replay(run_id=None, *, out_dir=None, deliver=False):
    """Re-render files and charts for a committed run, offline. Makes zero provider requests and never delivers."""
    conn = lake.connect(DB_PATH)
    lake.migrate(conn)
    run_id = run_id or _latest_run_id(conn)
    if not run_id:
        print("No committed snapshot to replay.")
        return 1
    ctx = lake.load_snapshot(conn, run_id)
    orig = lake.get_run(conn, run_id)
    # never re-render into the shared day folder: later runs that day and the delivered files live there
    out = Path(out_dir) if out_dir else DATA_DIR / ctx["run"]["run_folder"] / f"replay_{run_id}"
    artifacts, report, manifest, eml = render_outputs(conn, ctx, out)
    saved = json.loads(orig["artifacts_json"] or "{}").get("files", {})
    orig_report_sha = (saved.get("daily_market_report.txt") or {}).get("sha256")
    same = orig_report_sha == artifacts["daily_market_report.txt"]["sha256"] if orig_report_sha else None
    from send_email import load_eml, plain_body
    body_matches = plain_body(load_eml(out / "email.eml")).rstrip("\n") == report.rstrip("\n")
    replay_id = f"{run_id}-replay-{secrets.token_hex(2)}"
    lake.create_run(conn, replay_id, mode="replay", trigger="replay", run_folder=ctx["run"]["run_folder"],
                    code_version=code_version(), parent_run_id=run_id)
    lake.update_run(conn, replay_id, status="completed", finished_at=lake.utc_now_iso(), delivery_status="not_requested",
                    artifacts_json={"out_dir": str(out), "files": artifacts, "report_identical_to_original": same,
                                    "email_body_equals_report": body_matches})
    conn.close()
    print(f"🔁 Replayed {run_id} → {out}  (report identical to original: {same}; email body == report: {body_matches})")
    return 0


def resend(run_id=None):
    from send_email import deliver as smtp_deliver
    conn = lake.connect(DB_PATH)
    run_id = run_id or _latest_run_id(conn)
    r = lake.get_run(conn, run_id) if run_id else None
    if not r:
        print("No run to resend.")
        return 1
    # the day folder is shared by every run that day, so send this run's own stored copy, not the file on disk
    pid = (json.loads(r["artifacts_json"] or "{}").get("files", {}).get("email.eml") or {}).get("payload_id")
    _, content = lake.load_payload(conn, pid) if pid else (None, None)
    if content is None:
        print(f"No saved email for {run_id}")
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        eml = Path(tmp) / "email.eml"
        eml.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        status, detail = smtp_deliver(eml)
    lake.update_run(conn, run_id, delivery_status=status, delivery_detail=json.dumps({"smtp": detail, "resend": True}))
    print(f"✉️  Resend {run_id}: {status} — {detail}")
    return 0 if status == "smtp_accepted" else 2


def print_catalog(run_id=None):
    conn = lake.connect(DB_PATH)
    run_id = run_id or _latest_run_id(conn)
    ctx = lake.load_snapshot(conn, run_id) if run_id else {"run": {}}
    r = lake.get_run(conn, run_id) if run_id else None
    if r:
        ctx["charts"] = json.loads(r["artifacts_json"] or "{}").get("charts", [])
    print(catalog.lineage_markdown(ctx))
    return 0


def status():
    conn = lake.connect(DB_PATH, readonly=True)
    for r in conn.execute("SELECT run_id, mode, status, started_at, elapsed_s, delivery_status FROM v2_runs "
                          "ORDER BY started_at DESC LIMIT 10"):
        print(dict(r))
    if STATUS_PATH.exists():
        print("current:", STATUS_PATH.read_text())
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--offline", action="store_true", help="no provider/network requests")
    r.add_argument("--no-deliver", action="store_true", help="save report + email.eml but do not send/notify/upload")
    r.add_argument("--no-upload", action="store_true")
    r.add_argument("--skip-cme", action="store_true", help="use saved CME history only")
    r.add_argument("--no-cme-browser", action="store_true", help="plain HTTP only for CME (no browser window)")
    r.add_argument("--cme-max-files", type=int, default=10,
                   help="max missing CME volume workbooks to fetch this run (bounded backfill)")
    r.add_argument("--trigger", default="manual")
    r.add_argument("--login-wait", type=int, default=15, help="minutes to pause for a CME login if needed (0 = never)")
    rp = sub.add_parser("replay")
    rp.add_argument("--run")
    rp.add_argument("--out")
    sub.add_parser("import-history")
    rs = sub.add_parser("resend")
    rs.add_argument("--run")
    c = sub.add_parser("catalog")
    c.add_argument("--run")
    sub.add_parser("status")
    sub.add_parser("cme-login", help="log in to CME in a browser window (MFA) and save the session")
    sub.add_parser("ntfy-test", help="send the latest run's phone brief (summary + report attachment) to NTFY_URL")
    sa = sub.add_parser("signal-alerts", help="notify (ntfy + this Mac) for each Signal Watch row that just went from waiting to FIRED")
    sa.add_argument("--dry-run", action="store_true", help="print the live signal states and what would be sent; send and save nothing")
    sa.add_argument("--test", action="store_true", help="send the first signal as a sample alert to check delivery")
    a = p.parse_args(argv)
    if a.cmd in (None, "run"):
        a.offline = getattr(a, "offline", False)
        return run(offline=a.offline, deliver=not getattr(a, "no_deliver", False) and not a.offline,
                   upload=not getattr(a, "no_upload", False), trigger=getattr(a, "trigger", "manual"),
                   use_browser=not getattr(a, "no_cme_browser", False), skip_cme=getattr(a, "skip_cme", False),
                   max_volume_files=min(getattr(a, "cme_max_files", 10), config.CME_BACKFILL_MAX_ATTEMPTS),
                   login_wait_seconds=max(0, getattr(a, "login_wait", 15)) * 60)
    if a.cmd == "replay":
        return replay(a.run, out_dir=a.out)
    if a.cmd == "import-history":
        from core.importer import import_history
        import_history()
        return 0
    if a.cmd == "resend":
        return resend(a.run)
    if a.cmd == "catalog":
        return print_catalog(a.run)
    if a.cmd == "status":
        return status()
    if a.cmd == "signal-alerts":
        return signal_alerts(dry_run=a.dry_run, test=a.test)
    if a.cmd == "ntfy-test":
        from send_email import ntfy_brief
        conn = lake.connect(DB_PATH)
        rid = _latest_run_id(conn)
        if not rid:
            print("No committed run to test with.")
            return 1
        ctx = lake.load_snapshot(conn, rid)
        pid = (json.loads(lake.get_run(conn, rid)["artifacts_json"] or "{}").get("files", {})
               .get("daily_market_report.txt") or {}).get("payload_id")
        _, report = lake.load_payload(conn, pid) if pid else (None, None)
        conn.close()
        if report is None:
            print(f"No saved report for {rid}")
            return 1
        report = report.decode("utf-8") if isinstance(report, bytes) else report
        # test pushes do not upload: every upload is a permanent file on the server
        print(ntfy_brief(ctx, report, config.optional_env("DASHBOARD_URL", ""), None))
        return 0
    if a.cmd == "cme-login":
        from core import cme
        ok = cme.interactive_login(str(DATA_DIR / "state.json"), config.CME_LOGIN_USERNAME, config.CME_LOGIN_PASSWORD)
        print("✅ CME session saved and verified" if ok else "⚠️ CME download not verified (session saved if you logged in)")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
