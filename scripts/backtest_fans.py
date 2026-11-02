"""Walk-forward backtest of the scenario fans (Kronos vs the bootstrap baseline). OPERATOR-RUN: it calls Yahoo for
full daily history and writes lake observations to the database in PORTFOLIO_DATA_DIR.

For every Nth trading day since --start it simulates --paths paths 20 days ahead, scores the 5- and 20-day
horizons (p10-p90 band hit, CRPS as % of price, direction hit) and stores one observation per origin
(`fans.backtest_origin`, so a re-run skips finished origins), then one summary per series and model
(`fans.backtest`, read by GET /api/fans/scorecard). Additive only. Needs requirements-kronos.txt and the
weights (scripts/fetch_kronos.py) for the Kronos column; without them only the baseline is scored.

    PORTFOLIO_DATA_DIR=... .venv/bin/python scripts/backtest_fans.py [--start 2015-01-01] [--every 20]
                                                                       [--paths 30] [--series SPY,SLV] [--limit N]
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from core import fans, lake  # noqa: E402

HORIZONS = (5, 20)
SOURCE = "scripts/backtest_fans.py"


def history(sym):
    import yfinance as yf
    return fans._clean_ohlcv(yf.Ticker(sym).history(period="max", interval="1d"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--every", type=int, default=20)
    ap.add_argument("--paths", type=int, default=30)
    ap.add_argument("--series", default="")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many origins per series (smoke test)")
    a = ap.parse_args()
    config.ensure_data_dir()
    conn = lake.connect(config.DB_PATH)
    lake.migrate(conn)
    predictor, kinfo = fans.load_kronos()
    if predictor is None:
        print(f"Kronos skipped: {kinfo['reason']}")
    wanted = [s for s in a.series.split(",") if s] or list(fans.DAILY)
    done = {(r["entity"], r["dims_json"]) for r in conn.execute(
        "SELECT entity, dims_json FROM v2_observations WHERE metric_id = 'fans.backtest_origin'")}
    for sid in wanted:
        label, group, unit, sym = fans.DAILY[sid]
        df = history(sym)
        if df is None or len(df) < 600:
            print(f"{sid}: no history, skipped")
            continue
        close = df["close"].values
        first = int(np.searchsorted(df.index, pd.Timestamp(a.start)))
        origins = list(range(max(first, fans.LOOKBACK), len(df) - max(HORIZONS) - 1, a.every))
        if a.limit:
            origins = origins[:a.limit]
        for o in origins:
            day = df.index[o - 1].date().isoformat()          # the last bar the model may see
            dates = fans.trading_dates(df.index[o - 1].date(), max(HORIZONS))
            seed = fans._seed(sid, day)
            todo = [m for m in ("baseline", "kronos") if (m == "baseline" or predictor is not None)
                    and (sid, lake.dumps({"model": m, "origin": day})) not in done]
            if not todo:
                continue                                      # resumed: this origin is finished, skip the Kronos run too
            sources = {}
            for m in todo:
                try:
                    sources[m] = (fans.bootstrap_paths(close[:o], max(HORIZONS), seed, a.paths) if m == "baseline"
                                  else fans._kronos_paths(predictor, df.iloc[:o], dates, seed, a.paths))
                except Exception as e:                        # one bad origin must not end the run; the other model is still scored
                    print(f"{sid} {day} {m} skipped: {lake.redact(f'{type(e).__name__}: {e}')}", file=sys.stderr, flush=True)
            recs = []
            for m, paths in sources.items():
                if paths is None:
                    continue
                res = {"origin": day}
                for h in HORIZONS:
                    res[str(h)] = fans.score_origin(paths[:, h - 1], close[o - 1], close[o - 1 + h])
                recs.append(lake.obs("fans.backtest_origin", day, entity=sid, dims={"model": m, "origin": day}, value=res,
                                     source=SOURCE, derivation=fans.FANS_VERSION))
            lake.record_observations(conn, recs)
            print(f"{sid} {day} +{len(recs)}", flush=True)
        for m in ("baseline", "kronos"):
            rows = [lake.json.loads(r["value_json"]) for r in conn.execute(
                "SELECT value_json, dims_json FROM v2_observations WHERE metric_id = 'fans.backtest_origin' AND entity = ?", (sid,))
                if lake.json.loads(r["dims_json"]).get("model") == m]
            if rows:
                s = fans.summarise_scores(rows, HORIZONS)
                s["paths"] = a.paths
                lake.record_observations(conn, [lake.obs("fans.backtest", s["period"][1], entity=sid, dims={"model": m}, value=s,
                                                         source=SOURCE, derivation=fans.FANS_VERSION)])
                print(f"{sid} {m}: {s['by_horizon']}")


if __name__ == "__main__":
    main()
