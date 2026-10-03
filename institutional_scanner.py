"""Compatibility entry point for the SPY/SLV institutional scan.

The scan is part of the coordinated run (main_pipeline.py): trades and option chains are fetched once and the
equities_darkpool_gex_ledger rows are written by the coordinator. Running this file prints the latest
committed results; it no longer starts a separate, un-joined background collection.
"""
from core import lake, render
from config import DB_PATH


def run_institutional_scan():
    conn = lake.connect(DB_PATH)
    lake.migrate(conn)
    ctx = lake.load_snapshot(conn)
    conn.close()
    if ctx is None:
        return "No committed v2 snapshot yet. Run: python main_pipeline.py run"
    out = [f"\n========================================", " 🦅 INSTITUTIONAL ENGINE SCANNER",
           f" {ctx['run']['generated_local']} (run {ctx['run']['run_id']})", "========================================"]
    for sym in ("SPY", "SLV"):
        f, o = ctx["flow"][sym], (ctx["options"].get(sym) or {}).get("gex") or {}
        out.append(f"\n[{sym} OVERVIEW] - Spot: {ctx['prices'][sym.lower()].get('value')}")
        out.append(f"  Block-flow Bias:  {f.get('bias')} ({f.get('bias_method')}; VWAP {f.get('block_vwap')})")
        out.append(f"  Total Block Vol:  {f.get('block_volume')} shares  [{f.get('status')}] {f.get('reason') or ''}")
        out.append(f"  GEX Call Wall:    {o.get('call_wall')}   Put Wall: {o.get('put_wall')}   Zero Gamma: {o.get('zero_gamma')}")
    return "\n".join(out)


if __name__ == "__main__":
    print(run_institutional_scan())
