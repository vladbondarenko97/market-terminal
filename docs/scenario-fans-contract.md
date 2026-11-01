# Scenario fans: data contract (Phase 1)

Shared contract between the pipeline (core/fans.py) and the terminal (static/fans.js). The snapshot key is `fans`.

```
ctx["fans"] = {
  "status": "fresh" | "partial" | "missing" | "error", "reason": str|None, "version": "fans_v1",
  "kronos": {"status": "fresh"|"missing"|"error", "reason": str|None, "model": "Kronos-small", "params": "24.7M",
             "T": 1.0, "top_p": 0.9, "lookback": 400, "device": "mps"|"cpu", "seconds": float},
  "series": {
    "<id>": {
      "label": "SPY", "group": "markets"|"fuels"|"inventories"|"derived", "unit": "$"|"$/bbl"|"$/gal"|"%"|"kb"|"days"|"index",
      "freq": "D"|"W", "cards": [], "symbol": "SPY",   # Yahoo symbol when gradable, else null
      "asof": "YYYY-MM-DD", "last": float,
      "hist": {"dates": [...], "values": [...]},          # last 90 daily or 52 weekly points, for drawing
      "dates": [...],                                     # future dates: 20 NYSE trading days, or 8 weekly (EIA Fridays)
      "models": {
        "kronos":   {"status", "reason", "paths": [[float]*h]*100, "q": {"p10":[...], "p25":[...], "p50":[...], "p75":[...], "p90":[...]}},
        "baseline": {"status", "reason", "kind": "bootstrap"|"seasonal"|"joint_bootstrap", "paths": [[...]]*100, "q": {...}}
      },
      "levels": [{"v": float, "name": str}],              # optional reference lines (e.g. SPY gamma walls from this run)
      "note": str|None                                    # e.g. "experimental: weekly single values, not candles"
    }
  }
}
```

`cards` is `[]` in Phase 1 (the terminal decides where each series is drawn). Numbers are rounded to 4 significant digits. A model whose inputs are missing returns `{"status": "missing", "reason": ...}` with no paths.

Series ids (Phase 1):
- markets (daily, Kronos + bootstrap): SPY, SLV, VIX (^VIX), DXY (DX-Y.NYB), TNX (^TNX), SI (SI=F), GC (GC=F), HG (HG=F)
- fuels (daily, Kronos + bootstrap): HO (HO=F, ULSD diesel), RB (RB=F, gasoline), CL (CL=F, WTI), NG (NG=F, natural gas)
- derived (daily, joint bootstrap only in Phase 1): crack.diesel, crack.321
- inventories (weekly, Kronos experimental + seasonal bootstrap): eia.WDISTUS1, eia.WGTSTUS1, eia.WKJSTUS1, eia.WCESTUS1, eia.WCSSTUS1, eia.WPULEUS3
- derived weekly (seasonal joint bootstrap only): days.distillate, days.gasoline, days.jet

Routes (stored data only):
- `GET /api/fans` → the latest snapshot's `fans` block plus `run_id` and `generated_at`.
- `GET /api/fans/scorecard` → per series and model: `backtest` (stored by scripts/backtest_fans.py: n, band hit p10–p90, CRPS/price, direction hit, period) and `live` (graded v2_forecasts rows for models `kronos_fan_v1` and `baseline_fan_v1`: n, band hit 68, hit rate).
