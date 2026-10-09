"""v2 run coordinator: acquire -> capture -> calculate -> commit snapshot -> export -> deliver.

  python main_pipeline.py [run] [--offline] [--no-deliver] [--no-upload] [--skip-cme] [--no-cme-browser]
                          [--cme-max-files N] [--login-wait MINUTES] [--trigger NAME]
                                                                 # one run; `run` is the default command
  python main_pipeline.py cme-login                              # one-time CME login (MFA) for the volume FTP
  python main_pipeline.py replay [--run RUN_ID] [--out DIR]     # offline re-render of a saved run, no delivery
  python main_pipeline.py import-history [--no-backup]           # idempotent import of old files/tables
  python main_pipeline.py backfill-positions                     # record engine tickets for old live runs that have none
  python main_pipeline.py resend [--run RUN_ID]                  # explicit resend of a saved email.eml
  python main_pipeline.py catalog [--run RUN_ID]                 # variable catalog + lineage (Markdown)
  python main_pipeline.py status                                 # last 10 runs + the current run's status
  python main_pipeline.py ntfy-test                              # send the latest run's phone brief to NTFY_URL

Exit codes of `run`: 0 = finished cleanly (also when a scheduled run is skipped), 3 = finished with warnings (the
snapshot is committed, but a stage or a configured delivery channel failed; see `status`), 75 = another job holds
the run lock (nothing was started), 1 = failed before the snapshot was committed. A channel that is not configured
at all (no SMTP settings, no NTFY_URL, no UPLOAD_URL/UPLOAD_TOKEN) is skipped and is not a warning.
`run`, `cme-login`, `import-history` and `backfill-positions` take the run lock and exit 75 when it is held; `replay`,
`resend`, `catalog`, `status` and `ntfy-test` do not. `resend` exits 0 when SMTP accepted the message, 2 when it did
not, 1 when there is nothing to resend. `ntfy-test` exits 0 when the brief was sent and 2 when it was not.
An offline run writes its files to <data>/<day folder>/offline_<run id>/ and never to the shared day files.
"""
import argparse
import csv
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
from core.runlock import EXIT_BUSY, LOCK_PATH, STATUS_PATH, RunLock, busy_message, effective_state, lock_is_held, read_status  # noqa: F401

EXIT_WARNINGS = 3


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


def mark_interrupted_runs(conn):
    """Call while holding the run lock: any live or offline run still `running` belongs to a process that died, because
    the lock admits one run at a time. Only the status (and an empty error) is updated; no row is deleted."""
    with lake._WRITE_LOCK, conn:
        cur = conn.execute(
            "UPDATE v2_runs SET status = 'interrupted', "
            "error = COALESCE(error, 'the run ended without recording a result (process killed or crashed)') "
            "WHERE status = 'running' AND mode IN ('live', 'offline')")
    return cur.rowcount


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
def _stage_error(stages, warnings, name, exc):
    """A post-commit stage failed: keep the traceback in `stages`, add a warning, and let the other stages run."""
    import traceback
    stages[f"{name}_error"] = lake.redact(traceback.format_exc()[-3000:])
    warnings.append(f"{name} error: {lake.redact(str(exc))[:300]}")
    print(f"⚠️  Stage '{name}' failed: {exc}")


def run(*, offline=False, deliver=True, upload=True, trigger="manual", use_browser=True, skip_cme=False,
        max_volume_files=10, login_wait_seconds=900):
    """One coordinated run. Returns 0 (clean), 3 (completed with warnings) or 75 (busy); raises when the run fails
    before the snapshot is committed (the CLI then exits 1)."""
    if trigger == "scheduled":
        from core.market_calendar import scheduled_run_skip_reason
        skip = ("scheduled runs are off on this machine (SCHEDULED_RUNS=1 is not set in .env)"
                if not config.SCHEDULED_RUNS else scheduled_run_skip_reason())
        if skip:
            print(f"⏭️  Scheduled run skipped: {skip}")
            return 0
    deliver = deliver and not offline              # an offline run never sends or uploads anything
    config.ensure_data_dir(allow_create=True)      # creates only the default sibling CME_Data, never PORTFOLIO_DATA_DIR
    lock = RunLock()
    if not lock.acquire():
        print(busy_message())
        return EXIT_BUSY
    try:
        return _run_locked(offline=offline, deliver=deliver, upload=upload, trigger=trigger, use_browser=use_browser,
                           skip_cme=skip_cme, max_volume_files=max_volume_files, login_wait_seconds=login_wait_seconds)
    finally:
        lock.release()


def _run_locked(*, offline, deliver, upload, trigger, use_browser, skip_cme, max_volume_files, login_wait_seconds):
    from core import collect
    from core.sources import SourceSession
    from send_email import alphaflow_appendix, ntfy_push
    t0 = time.monotonic()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    run_id = f"{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
    folder = run_folder_name(now)
    mode = "offline" if offline else "live"
    # an offline run never touches the day's shared files: its own folder inside the day folder
    out_dir = DATA_DIR / folder / (f"offline_{run_id}" if offline else "")
    conn = lake.connect(DB_PATH)
    try:
        lake.migrate(conn)
        stale = mark_interrupted_runs(conn)
        if stale:
            print(f"ℹ️  {stale} earlier run(s) never recorded an end (process killed); marked 'interrupted'.")
        lake.create_run(conn, run_id, mode=mode, trigger=trigger, run_folder=folder, code_version=code_version())
        stages = {}
        set_status(run_id=run_id, stage="collecting", state="running", pid=os.getpid(), mode=mode,
                   started_at=now.isoformat(), finished_at=None, elapsed_s=None, error=None)
        print(f"🚀 v2 run {run_id} ({mode}, trigger={trigger}) → {out_dir}")
        try:
            session = SourceSession(conn, run_id, offline=offline)
            run_meta = {"run_id": run_id, "generated_at": now.isoformat(), "run_folder": folder, "mode": mode,
                        "code_version": code_version(), "trigger": trigger}
            notify = (lambda m: ntfy_push([("CME login needed", m, "urgent", "key")])) if deliver else None
            set_status(stage="collecting (a CME login window may be waiting for you)")
            ctx, frames = collect.build_context(conn, session, run_meta, use_browser=use_browser,
                                               skip_cme_download=skip_cme, max_volume_files=max_volume_files,
                                               login_wait_seconds=login_wait_seconds, notify=notify)
            ctx["appendix"] = {"alphaflow_text": alphaflow_appendix()}
            stages["collect"] = {"status": "done", "seconds": round(time.monotonic() - t0, 1),
                                 "detail": ctx["stage_seconds"]}
            set_status(stage="capturing")
            n_obs = lake.record_observations(conn, collect.context_observations(ctx, frames), run_id=run_id)
            stages["capture"] = {"status": "done", "new_observations": n_obs}
            lake.commit_snapshot(conn, run_id, ctx)
            ctx = lake.load_snapshot(conn, run_id)     # every renderer consumes the committed snapshot
            stats = session.stats()
            lake.update_run(conn, run_id, status="committed", stages_json=stages,
                            request_counts_json=lake.redact(lake.dumps(stats)),
                            sources_json={k: {"status": v.get("status"), "reason": v.get("reason")}
                                          for k, v in ctx.items() if isinstance(v, dict) and "status" in v})
            print(f"💾 Snapshot committed ({n_obs} new observations, provider calls {stats['calls']}, "
                  f"dedup hits {stats['dedup_hits']})")
        except Exception as e:
            import traceback
            elapsed = time.monotonic() - t0
            lake.update_run(conn, run_id, status="failed", error=lake.redact(traceback.format_exc()[-4000:]),
                            stages_json=stages, elapsed_s=elapsed, finished_at=lake.utc_now_iso())
            set_status(stage="failed", state="failed", error=lake.redact(str(e)), finished_at=lake.utc_now_iso(),
                       elapsed_s=round(elapsed, 1))
            print(f"❌ Run failed before the snapshot was committed: {e}")
            raise
        warnings = _post_commit(conn, ctx, run_id, mode, out_dir, stages, deliver=deliver, upload=upload)
        elapsed = time.monotonic() - t0
        final = "completed_with_warnings" if warnings else "completed"
        if warnings:
            stages["warnings"] = warnings
        lake.update_run(conn, run_id, status=final, stages_json=stages, finished_at=lake.utc_now_iso(),
                        elapsed_s=elapsed, error=lake.redact("; ".join(warnings)) or None,
                        request_counts_json=lake.redact(lake.dumps(session.stats())))
        set_status(stage="done", state=final, finished_at=lake.utc_now_iso(), elapsed_s=round(elapsed, 1),
                   error=lake.redact("; ".join(warnings)) or None)
        print(f"🎉 Run {run_id} {final} in {elapsed:.1f}s" + (f" — {'; '.join(warnings)}" if warnings else ""))
        return EXIT_WARNINGS if warnings else 0
    finally:
        conn.close()


def _post_commit(conn, ctx, run_id, mode, out_dir, stages, *, deliver, upload):
    """Everything after the snapshot is committed. Ledgers, trade signal, forecast log, rendering and delivery each
    run on their own: a failure is recorded in `stages` and returned as a warning, and the others still run."""
    warnings = []
    set_status(stage="exporting")
    rows = None
    if mode == "live":
        try:
            rows = export_ledgers(conn, ctx, run_id, stages)
            bad = {k: v for k, v in stages["ledgers"].items() if str(v).startswith("error")}
            if bad:
                warnings.append(f"ledger errors: {bad}")
        except Exception as e:
            _stage_error(stages, warnings, "ledgers", e)
        try:
            from core import positions
            sig = positions.record_signal(conn, ctx)
            stages["trade_signal"] = sig["position_type"] if sig else "none"
        except Exception as e:
            _stage_error(stages, warnings, "trade_signal", e)
        try:
            from core import forecast as _fc
            stages["forecasts_logged"] = _fc.record_forecasts(conn, ctx)
        except Exception as e:
            _stage_error(stages, warnings, "forecast_log", e)
    else:
        stages["ledgers"] = "skipped (offline run does not append legacy ledgers)"
    rendered = False
    report = None
    try:
        artifacts, report, manifest, eml = render_outputs(conn, ctx, out_dir, rows=rows)
        rendered = True
        stages["export"] = {"status": "done", "charts": {c["file"]: c["status"] for c in manifest}}
        print(f"📁 Exports written to {out_dir}")
    except Exception as e:
        _stage_error(stages, warnings, "render", e)
    if rendered:
        try:
            rpid = lake.store_payload(conn, source="v2_render", kind="daily_market_report", content=report, fmt="text",
                                      run_id=run_id)
            epid = lake.store_payload(conn, source="v2_render", kind="email_mime", content=eml, fmt="eml",
                                      run_id=run_id)
            artifacts["daily_market_report.txt"]["payload_id"] = rpid
            artifacts["email.eml"]["payload_id"] = epid
        except Exception as e:
            _stage_error(stages, warnings, "payload_store", e)
        try:
            lake.update_run(conn, run_id, artifacts_json={"out_dir": str(out_dir), "files": artifacts,
                                                          "charts": [{k: v for k, v in c.items() if k != "trace"}
                                                                     for c in manifest]},
                            stages_json=stages)
        except Exception as e:
            _stage_error(stages, warnings, "artifacts", e)
    if not deliver:
        lake.update_run(conn, run_id, delivery_status="not_requested")
    elif not rendered:
        lake.update_run(conn, run_id, delivery_status="failed",
                        delivery_detail=json.dumps({"error": "nothing was rendered, so there was nothing to send"}))
        warnings.append("delivery skipped: render failed")
    else:
        set_status(stage="delivering")
        try:
            lake.update_run(conn, run_id, delivery_status="attempting")
            warnings.extend(_deliver(conn, ctx, run_id, out_dir, report, upload=upload))
        except Exception as e:
            _stage_error(stages, warnings, "delivery", e)
    return warnings


def _deliver(conn, ctx, run_id, out_dir, report, *, upload):
    """Email, report upload, phone pushes, then the database/dashboard upload. Every channel is attempted whatever
    the others did. A channel that is not configured is `skipped`; one that is configured and fails is a warning.
    Records `delivery_status` (the email outcome) and `delivery_detail` (all channels). Returns the warnings."""
    import upload_data
    from send_email import deliver as smtp_deliver, ntfy_brief, ntfy_push
    warnings = []

    def failure(e):
        return lake.redact(f"{type(e).__name__}: {e}")[:300]

    try:
        email_status, email_detail = smtp_deliver(out_dir / "email.eml")
    except Exception as e:
        email_status, email_detail = "failed", failure(e)
    print(f"✉️  Email: {email_status} — {email_detail}")
    if email_status not in ("smtp_accepted", "skipped"):
        warnings.append(f"email {email_status}: {lake.redact(str(email_detail))[:200]}")

    report_up = {"status": "not_requested"}
    if upload:
        try:
            report_up = upload_data.upload_report_result(out_dir / "daily_market_report.txt",
                                                         upload_data.report_filename(ctx))
        except Exception as e:
            report_up = {"status": "failed", "detail": failure(e)}
        if report_up["status"] == "failed":
            warnings.append(f"report upload failed: {report_up.get('detail')}")

    pushes = []
    try:
        pushes.append(ntfy_brief(ctx, report, config.DASHBOARD_URL, report_up.get("url")))
    except Exception as e:
        pushes.append({"title": "daily brief", "status": "failed", "error": failure(e)})
    try:
        pushes.extend(ntfy_push(_refinery_alerts(ctx)))
    except Exception as e:
        pushes.append({"title": "refinery alerts", "status": "failed", "error": failure(e)})
    bad = [p for p in pushes if p.get("status") in ("failed", "outcome_unknown")]
    if bad:
        warnings.append(f"phone push {bad[0]['status']}: {bad[0].get('error')}"
                        + (f" (+{len(bad) - 1} more)" if len(bad) > 1 else ""))

    up_status, up_detail = "not_requested", None
    if upload:
        try:
            up_status, up_detail = upload_data.upload_files(str(out_dir))
        except Exception as e:
            up_status, up_detail = "failed", failure(e)
        if up_status == "failed":
            warnings.append(f"upload failed: {up_detail}")

    lake.update_run(conn, run_id, delivery_status=email_status,
                    delivery_detail=json.dumps({"smtp": email_detail, "ntfy": pushes, "report_upload": report_up,
                                                "upload": [up_status, up_detail]}))
    return warnings


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
    held = lock_is_held()
    conn = lake.connect(DB_PATH, readonly=True)
    for r in conn.execute("SELECT run_id, mode, status, started_at, elapsed_s, delivery_status FROM v2_runs "
                          "ORDER BY started_at DESC LIMIT 10"):
        row = dict(r)
        if row["status"] == "running" and row["mode"] in ("live", "offline") and not held:
            row["status"] = "interrupted"            # the process is gone; the next run records this in the table
        print(row)
    conn.close()
    cur = read_status()
    if cur:
        cur["state"] = effective_state(cur)         # "running" with no process holding the run lock = "interrupted"
        print("current:", json.dumps(cur, indent=2))
    return 0


def import_history_command(*, backup=True):
    """`import-history`: takes the run lock, backs portfolio.db up first (unless backup=False), then imports."""
    from core.importer import import_history
    config.ensure_data_dir()
    lock = RunLock()
    if not lock.acquire():
        print(busy_message())
        return EXIT_BUSY
    try:
        if backup and not backup_database_file():
            return 1
        import_history()
        return 0
    finally:
        lock.release()


def backup_database_file():
    """Copy portfolio.db to <data>/backups/portfolio-<UTC time>.db (SQLite backup API plus an integrity check).
    Returns the path, True when there is nothing to back up yet, or None when the backup failed."""
    if not DB_PATH.exists():
        print("💾 No portfolio.db yet: nothing to back up.")
        return True
    dest_dir = DATA_DIR / "backups"
    dest = dest_dir / f"portfolio-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.db"
    try:
        dest_dir.mkdir(exist_ok=True)
        lake.backup_database(DB_PATH, dest)
    except Exception as e:
        dest.unlink(missing_ok=True)
        print(f"❌ Backup of portfolio.db failed ({e}). Nothing was imported. "
              "Fix the cause, or run again with --no-backup to import without a backup.")
        return None
    print(f"💾 Backup of portfolio.db → {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
    return dest


def backfill_positions():
    """`backfill-positions`: record engine tickets for committed live runs that have none. Idempotent."""
    from core import positions
    config.ensure_data_dir()
    lock = RunLock()
    if not lock.acquire():
        print(busy_message())
        return EXIT_BUSY
    try:
        conn = lake.connect(DB_PATH)
        lake.migrate(conn)
        before = conn.execute("SELECT COUNT(*) FROM v2_trade_signals").fetchone()[0]
        with_ticket = positions.backfill(conn)
        after = conn.execute("SELECT COUNT(*) FROM v2_trade_signals").fetchone()[0]
        conn.close()
        print(f"📈 {with_ticket} committed live run(s) carry an engine ticket; {after - before} new position row(s) "
              f"recorded ({after} in total). Runs that already had a row were left alone.")
        return 0
    finally:
        lock.release()


def _database_ready():
    if DB_PATH.exists():
        return True
    print(f"No database at {DB_PATH} yet. Run `python main_pipeline.py run` first.")
    return False


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="one coordinated run: collect, snapshot, render, deliver (the default command)")
    r.add_argument("--offline", action="store_true",
                   help="no provider/network requests; implies --no-deliver and records no ledgers, positions or forecasts. "
                        "Files go to <data>/<day folder>/offline_<run id>/, never over the day's files")
    r.add_argument("--no-deliver", action="store_true",
                   help="save report + email.eml but do not send, notify (ntfy) or upload; ledgers and positions are still recorded")
    r.add_argument("--no-upload", action="store_true",
                   help="skip both uploads (the report and the database/dashboard copy); email and ntfy still go out")
    r.add_argument("--skip-cme", action="store_true",
                   help="make no CME requests (volume or inventory); use saved CME history only")
    r.add_argument("--no-cme-browser", action="store_true",
                   help="keep the CME browser closed: no volume listing or download (volume outcome `skipped`), and the "
                        "inventory workbook is fetched over plain HTTP only. Log in with `cme-login` and run without "
                        "this flag to fetch volume files")
    r.add_argument("--cme-max-files", type=int, default=10,
                   help="max missing CME volume workbooks to fetch this run (default 10, capped at 40; 0 = skip the volume download)")
    r.add_argument("--trigger", default="manual",
                   help="label stored with the run (default manual). Only `scheduled` is special: that run is skipped "
                        "unless SCHEDULED_RUNS=1 is set in .env and now is inside the scheduler's window on an NYSE "
                        "trading day; a skipped run exits 0 and records nothing")
    r.add_argument("--login-wait", type=int, default=15,
                   help="minutes to wait for you to finish a CME login when CME refuses a download (default 15; 0 = never wait)")
    rp = sub.add_parser("replay", help="re-render a saved run offline (no network, no delivery)")
    rp.add_argument("--run", help="run id to replay (default: the latest committed snapshot)")
    rp.add_argument("--out", help="output folder (default: <data folder>/<day folder>/replay_<run id>)")
    ih = sub.add_parser("import-history", help="import old files and tables into the lake (idempotent); takes the run "
                                               "lock and first copies portfolio.db to <data>/backups/")
    ih.add_argument("--no-backup", action="store_true", help="do not back portfolio.db up before importing")
    sub.add_parser("backfill-positions", help="record engine tickets for committed live runs that have none "
                                              "(idempotent; takes the run lock)")
    rs = sub.add_parser("resend", help="send a saved run's email.eml again (explicit; never automatic)")
    rs.add_argument("--run", help="run id to resend (default: the latest committed snapshot)")
    c = sub.add_parser("catalog", help="print the variable catalog + lineage (Markdown) for a run")
    c.add_argument("--run", help="run id (default: the latest committed snapshot)")
    sub.add_parser("status", help="print the last 10 runs and the current run's status ('interrupted' = the run died)")
    sub.add_parser("cme-login", help="log in to CME in a browser window (MFA) and save the session (takes the run lock)")
    sub.add_parser("ntfy-test", help="send the latest run's phone brief (summary + report attachment) to NTFY_URL; "
                                     "exits 2 when it was not sent")
    a = p.parse_args(argv)
    try:
        return _dispatch(a)
    except config.ConfigError as e:
        print(f"ConfigError: {e}", file=sys.stderr)
        return 1


def _dispatch(a):
    if a.cmd in (None, "run"):
        a.offline = getattr(a, "offline", False)
        return run(offline=a.offline, deliver=not getattr(a, "no_deliver", False) and not a.offline,
                   upload=not getattr(a, "no_upload", False), trigger=getattr(a, "trigger", "manual"),
                   use_browser=not getattr(a, "no_cme_browser", False), skip_cme=getattr(a, "skip_cme", False),
                   max_volume_files=min(getattr(a, "cme_max_files", 10), config.CME_BACKFILL_MAX_ATTEMPTS),
                   login_wait_seconds=max(0, getattr(a, "login_wait", 15)) * 60)
    if a.cmd == "import-history":
        return import_history_command(backup=not a.no_backup)
    if a.cmd == "backfill-positions":
        return backfill_positions() if _database_ready() else 1
    if a.cmd == "cme-login":
        from core import cme
        config.ensure_data_dir(allow_create=True)
        lock = RunLock()
        if not lock.acquire():
            print(busy_message())
            return EXIT_BUSY
        try:
            ok = cme.interactive_login(str(DATA_DIR / "state.json"), config.CME_LOGIN_USERNAME, config.CME_LOGIN_PASSWORD)
        finally:
            lock.release()
        print("✅ CME session saved and verified" if ok else "⚠️ CME download not verified (session saved if you logged in)")
        return 0 if ok else 1
    # the remaining commands only read an existing database
    if not _database_ready():
        return 1
    if a.cmd == "replay":
        return replay(a.run, out_dir=a.out)
    if a.cmd == "resend":
        return resend(a.run)
    if a.cmd == "catalog":
        return print_catalog(a.run)
    if a.cmd == "status":
        return status()
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
        result = ntfy_brief(ctx, report, config.DASHBOARD_URL, None)
        print(result)
        return 0 if result.get("status") in ("sent", "sent_without_attachment") else 2


if __name__ == "__main__":
    sys.exit(main())
