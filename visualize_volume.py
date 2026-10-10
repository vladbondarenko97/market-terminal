"""Chart jobs. Each job reads saved history (lake + legacy tables) as of a run, renders one PNG, and returns a
manifest entry: generated | stale_input | missing_input | render_error. No provider requests are made here.

Design: one y-scale per panel (no dual axes). Two measures of different scale are stacked panels sharing
the date axis. Light surface for email; categorical slots 1-3 of the validated reference palette.
"""
import os
import traceback

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.ticker import FuncFormatter

from core import history
from core.catalog import CHARTS

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3de"
S1, S2, S3 = "#2a78d6", "#eb6834", "#1baf7a"
WARN = "#b25e00"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": INK_2, "xtick.color": INK_2, "ytick.color": INK_2,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "axes.axisbelow": True,
    "axes.spines.top": False, "axes.spines.right": False, "font.size": 10, "axes.titlesize": 11,
    "axes.titleweight": "bold", "axes.titlecolor": INK, "axes.titlelocation": "left", "legend.frameon": False,
    "lines.linewidth": 2,
})


class MissingInput(Exception):
    pass


def _human(x, _=None):
    ax = abs(x)
    if ax >= 1e9:
        return f"{x / 1e9:.1f}B"
    if ax >= 1e6:
        return f"{x / 1e6:.2f}".rstrip("0").rstrip(".") + "M"
    if ax >= 1e3:
        return f"{x / 1e3:.0f}K"
    return f"{x:g}"


def _figure(n_panels, title, subtitle):
    lines = subtitle.split("\n")
    head = 0.75 + 0.2 * len(lines)
    h = 3.2 * n_panels + head
    fig, axes = plt.subplots(n_panels, 1, figsize=(12, h), sharex=True, squeeze=False)
    fig.text(0.01, 1 - 0.18 / h, title, ha="left", va="top", fontsize=14, fontweight="bold", color=INK)
    for i, line in enumerate(lines):
        fig.text(0.01, 1 - (0.55 + 0.2 * i) / h, line, ha="left", va="top", fontsize=9.5,
                 color=WARN if line.startswith("⚠") else INK_2)
    fig._head_inches = head
    return fig, [a[0] for a in axes]


def _finish(fig, axes, path, dates):
    ax = axes[-1]
    n = len(dates)
    if n <= 14:
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=1 if n <= 8 else 2))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    else:
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=10))
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax.xaxis.get_major_locator()))
    if n == 1:
        d = pd.Timestamp(dates[0])
        ax.set_xlim(d - pd.Timedelta(days=3), d + pd.Timedelta(days=3))
    fig.tight_layout(rect=(0, 0, 1, 1 - (fig._head_inches + 0.05) / fig.get_figheight()))
    tmp = path + ".tmp.png"
    fig.savefig(tmp, dpi=110)
    plt.close(fig)
    os.replace(tmp, path)


def _bars(ax, x, y, color, label):
    width = 0.7 if len(x) <= 40 else 0.9
    ax.bar(x, y, width=width, color=color, label=label, edgecolor=SURFACE, linewidth=1)
    ax.yaxis.set_major_formatter(FuncFormatter(_human))


def _line(ax, x, y, color, label, markers):
    ax.plot(x, y, color=color, label=label, marker="o" if markers else None, markersize=6,
            markeredgecolor=SURFACE, markeredgewidth=1.5)
    ax.yaxis.set_major_formatter(FuncFormatter(_human))


def _label_last(ax, x, y, text, color):
    pts = [(a, b) for a, b in zip(x, y) if b is not None and not pd.isna(b)]
    if pts:
        ax.annotate(text, pts[-1], xytext=(6, 0), textcoords="offset points", va="center", fontsize=9, color=INK_2)


# ============================================================ data access
def _sessions(conn, key, as_of, n):
    s = history.cme_series(conn, key, as_of)
    if not s:
        raise MissingInput(f"no CME history for {key}")
    return pd.DataFrame(s[-n:]).assign(date=lambda d: pd.to_datetime(d["date"]))


def _days_cut(df, col, end, n):
    lo = pd.Timestamp(end) - pd.Timedelta(days=n)
    return df[df[col] >= lo]


# ============================================================ jobs
def job_conviction(conn, ctx, spec, win, window, path):
    key = spec["inputs"][0].split(":")[1]
    df = _sessions(conn, key, ctx["run"]["generated_at"], window[1])
    fig, (a1, a2) = _figure(2, spec["title"], _subtitle(ctx, df["date"], window, "cme"))
    _bars(a1, df["date"], df["volume"], S1, "Volume")
    a1.set_title("Total volume (contracts)")
    _line(a2, df["date"], df["open_interest"], S1, "Open interest", len(df) <= 40)
    a2.set_title("Open interest (contracts)")
    _finish(fig, [a1, a2], path, df["date"])
    return df["date"]


def job_divergence(conn, ctx, spec, win, window, path):
    k1, k2 = spec["inputs"][0].split(":")[1], spec["inputs"][1].split(":")[1]
    a = _sessions(conn, k1, ctx["run"]["generated_at"], window[1])
    b = _sessions(conn, k2, ctx["run"]["generated_at"], window[1])
    m = a[["date", "open_interest"]].merge(b[["date", "open_interest"]], on="date", suffixes=("_std", "_micro")).dropna()
    if len(m) < spec["min_points"]:
        raise MissingInput(f"fewer than {spec['min_points']} common sessions for {k1}/{k2}")
    base = m.iloc[0]
    m["std_idx"] = m["open_interest_std"] / base["open_interest_std"] * 100
    m["micro_idx"] = m["open_interest_micro"] / base["open_interest_micro"] * 100
    labels = {"SI_F": ("Standard SI (5,000 oz)", "Micro SIL (1,000 oz)"),
              "ES_F": ("Standard ES", "Micro MES")}[k1]
    extra = spec["family"] == 9
    dp = None
    if extra:
        dp = history.flow_ledger_daily(conn, "SPY", ctx["run"]["generated_at"])
    n = 2 if extra and dp is not None and not dp.empty else 1
    sub = _subtitle(ctx, m["date"], window, "cme") + f"  ·  Base = 100 on {base['date']:%Y-%m-%d}"
    fig, axes = _figure(n, spec["title"], sub)
    ax = axes[0]
    ax.axhline(100, color=INK_2, linewidth=1, linestyle=":")
    _line(ax, m["date"], m["std_idx"], S1, labels[0], len(m) <= 40)
    _line(ax, m["date"], m["micro_idx"], S2, labels[1], len(m) <= 40)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}"))
    ax.set_title("Open interest index (size-based proxy, not identified ownership)")
    ax.legend(loc="upper left", ncols=2)
    _label_last(ax, m["date"], m["std_idx"], labels[0].split(" (")[0], S1)
    _label_last(ax, m["date"], m["micro_idx"], labels[1].split(" (")[0], S2)
    if n == 2:
        dp = dp.assign(date=dp["_ts"].dt.normalize())
        dp = dp[(dp["date"] >= m["date"].min()) & (dp["date"] <= m["date"].max())]
        _bars(axes[1], dp["date"], pd.to_numeric(dp["DP_Total_Vol"], errors="coerce"), S3, "SPY block volume")
        axes[1].set_title("SPY block volume by scan date (DBEQ.BASIC, prints ≥ 10,000 sh; venue not verified)")
    _finish(fig, axes, path, m["date"])
    return m["date"]


def job_es_options(conn, ctx, spec, win, window, path):
    c = _sessions(conn, "ES_C", ctx["run"]["generated_at"], window[1])
    p = _sessions(conn, "ES_P", ctx["run"]["generated_at"], window[1])
    m = c[["date", "volume"]].merge(p[["date", "volume"]], on="date", suffixes=("_call", "_put"))
    m["ratio"] = m["volume_put"] / m["volume_call"].where(m["volume_call"] > 0)
    fig, (a1, a2) = _figure(2, spec["title"], _subtitle(ctx, m["date"], window, "cme") +
                            "  ·  Source: CME daily volume report (not SPY equity options)")
    off = pd.Timedelta(hours=7)
    a1.bar(m["date"] - off, m["volume_call"], width=0.4, color=S1, label="Calls", edgecolor=SURFACE)
    a1.bar(m["date"] + off, m["volume_put"], width=0.4, color=S2, label="Puts", edgecolor=SURFACE)
    a1.yaxis.set_major_formatter(FuncFormatter(_human))
    a1.legend(loc="upper left", ncols=2)
    a1.set_title("Option volume (contracts)")
    a2.axhline(1.0, color=INK_2, linewidth=1, linestyle=":")
    _line(a2, m["date"], m["ratio"], S1, "Put/Call", len(m) <= 40)
    a2.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.2f}"))
    a2.set_title("Put/call volume ratio (gaps where call volume is zero)")
    _finish(fig, [a1, a2], path, m["date"])
    return m["date"]


def job_zn(conn, ctx, spec, win, window, path):
    df = _sessions(conn, "ZN_F", ctx["run"]["generated_at"], window[1])
    fig, (a1, a2) = _figure(2, spec["title"], _subtitle(ctx, df["date"], window, "cme") +
                            "  ·  Futures activity, not the 10Y yield itself")
    _bars(a1, df["date"], df["volume"], S1, "Volume")
    a1.set_title("Total volume (contracts)")
    a2.axhline(0, color=INK_2, linewidth=1)
    _bars(a2, df["date"], df["oi_change"], S1, "OI change")
    a2.set_title("Daily open-interest change (contracts; vs previous session)")
    _finish(fig, [a1, a2], path, df["date"])
    return df["date"]


def job_inventory(conn, ctx, spec, win, window, path):
    s = pd.DataFrame(history.inventory_series(conn, ctx["run"]["generated_at"]))
    if s.empty:
        raise MissingInput("no saved inventory reports")
    s["date"] = pd.to_datetime(s["date"])
    last = s["date"].max()
    s = _days_cut(s, "date", last, window[1]).dropna(subset=["registered"])
    fig, (ax,) = _figure(1, spec["title"], _subtitle(ctx, s["date"], window, "inventory") +
                         "  ·  Report dates, troy ounces")
    _line(ax, s["date"], s["eligible"] / 1e6, S2, "Eligible", len(s) <= 40)
    _line(ax, s["date"], s["registered"] / 1e6, S1, "Registered", len(s) <= 40)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}M"))
    ax.set_ylim(bottom=0)
    ax.set_title("Million troy ounces")
    ax.legend(loc="lower left", ncols=2)
    _label_last(ax, s["date"], s["registered"] / 1e6, f"{s['registered'].iloc[-1] / 1e6:.2f}M", S1)
    _label_last(ax, s["date"], s["eligible"] / 1e6, f"{s['eligible'].iloc[-1] / 1e6:.2f}M", S2)
    _finish(fig, [ax], path, s["date"])
    return s["date"]


def _crypto(conn, ctx, window):
    df = history.crypto_daily(conn, ctx["run"]["generated_at"])
    if df.empty:
        raise MissingInput("crypto_metrics_history is empty")
    df = df.assign(date=df["_ts"].dt.normalize())
    end = pd.Timestamp(ctx["run"]["generated_at"]).tz_convert("America/Chicago").tz_localize(None).normalize()
    return _days_cut(df, "date", end, window[1])


def job_crypto_ratios(conn, ctx, spec, win, window, path):
    df = _crypto(conn, ctx, window)
    fig, (a1, a2) = _figure(2, spec["title"], _subtitle(ctx, df["date"], window, None) +
                            "  ·  Weekend rows carry the prior metals close")
    _line(a1, df["date"], df["Silver_BTC_Ratio"], S1, "Silver oz per BTC", len(df) <= 40)
    a1.set_title("Silver ounces per BTC (BTC-USD / SI=F)")
    _line(a2, df["date"], df["Gold_BTC_Ratio"], S1, "Gold oz per BTC", len(df) <= 40)
    a2.set_title("Gold ounces per BTC (BTC-USD / GC=F)")
    _finish(fig, [a1, a2], path, df["date"])
    return df["date"]


def job_metals(conn, ctx, spec, win, window, path):
    df = _crypto(conn, ctx, window)
    fig, (a1, a2) = _figure(2, spec["title"], _subtitle(ctx, df["date"], window, None) +
                            "  ·  Continuous front-month futures, USD/oz")
    _line(a1, df["date"], df["Silver_Price"], S1, "Silver", len(df) <= 40)
    a1.set_title("Silver (SI=F), USD/oz")
    a1.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:,.2f}"))
    _line(a2, df["date"], df["Gold_Price"], S1, "Gold", len(df) <= 40)
    a2.set_title("Gold (GC=F), USD/oz")
    a2.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:,.0f}"))
    _finish(fig, [a1, a2], path, df["date"])
    return df["date"]


def _macro(conn, ctx, window):
    df = history.macro_daily(conn, ctx["run"]["generated_at"])
    if df.empty:
        raise MissingInput("macro_master_ledger is empty")
    df = df.assign(date=df["_ts"].dt.normalize())
    end = pd.Timestamp(ctx["run"]["generated_at"]).tz_convert("America/Chicago").tz_localize(None).normalize()
    return _days_cut(df, "date", end, window[1])


def job_credit(conn, ctx, spec, win, window, path):
    df = _macro(conn, ctx, window)
    cols = [("High_Yield_OAS", "High-yield OAS (percent, FRED BAMLH0A0HYM2)", "{:.2f}%"),
            ("10Y_Yield", "10Y Treasury yield (percent, ^TNX)", "{:.2f}%"),
            ("Reverse_Repo_BN", "ON reverse repo (USD bn, FRED RRPONTSYD)", "${:,.1f}B")]
    cols = [c for c in cols if c[0] in df.columns and pd.to_numeric(df[c[0]], errors="coerce").notna().any()]
    if not cols:
        raise MissingInput("no OAS / yield / RRP values in window")
    fig, axes = _figure(len(cols), spec["title"], _subtitle(ctx, df["date"], window, None) +
                        "  ·  One value per ledger day (last run of the day)")
    for ax, (col, title, fmt) in zip(axes, cols):
        _line(ax, df["date"], pd.to_numeric(df[col], errors="coerce"), S1, title, len(df) <= 40)
        ax.set_title(title)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _, fmt=fmt: fmt.format(v)))
    _finish(fig, axes, path, df["date"])
    return df["date"]


def job_squeeze(conn, ctx, spec, win, window, path):
    df = _macro(conn, ctx, window)
    df = df.assign(prem=pd.to_numeric(df.get("SHFE_Premium"), errors="coerce")).dropna(subset=["prem"])
    inv = pd.DataFrame(history.inventory_series(conn, ctx["run"]["generated_at"]))
    if df.empty or inv.empty:
        raise MissingInput("SGE premium history or inventory reports missing")
    inv["date"] = pd.to_datetime(inv["date"])
    inv = inv.dropna(subset=["registered_net_change"]).sort_values("date")
    inv["reg_smooth"] = inv["registered_net_change"].rolling(5, min_periods=1).mean()
    inv = _days_cut(inv, "date", df["date"].max(), window[1])
    fig, (a1, a2) = _figure(2, spec["title"], _subtitle(ctx, df["date"], window, "inventory") +
                            "  ·  Premium uses a 13% VAT assumption")
    a1.axhline(0, color=INK_2, linewidth=1)
    _line(a1, df["date"], df["prem"], S1, "SGE premium", len(df) <= 40)
    a1.set_title("SGE tax-adjusted premium over SI=F (USD/oz)")
    a1.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:,.2f}"))
    a2.axhline(0, color=INK_2, linewidth=1)
    _bars(a2, inv["date"], inv["reg_smooth"], S1, "Registered net change (5-report avg)")
    a2.set_title("COMEX registered net change, 5-report average (troy oz; excludes adjustments)")
    _finish(fig, [a1, a2], path, df["date"])
    return df["date"]


JOBS = {1: job_conviction, 2: job_conviction, 3: job_divergence, 4: job_es_options, 5: job_zn, 6: job_inventory,
        7: job_crypto_ratios, 8: job_metals, 9: job_divergence, 10: job_credit, 11: job_squeeze}


def _window_label(window):
    kind, n = window
    return f"Last {n} CME sessions" if kind == "sessions" else ("Last 1 year" if n == 365 else f"Last {n} days")


def _subtitle(ctx, dates, window, dep):
    d = pd.to_datetime(pd.Series(list(dates)))
    rng = f"{d.min():%Y-%m-%d} → {d.max():%Y-%m-%d}, {len(d)} point{'s' if len(d) != 1 else ''}" if len(d) else "no data"
    head = f"{_window_label(window)}  ·  {rng}"
    stale = _stale_reason(ctx, dep)
    return f"⚠ STALE INPUT — {stale}\n{head}" if stale else head


def _stale_reason(ctx, dep):
    if dep == "cme" and ctx["cme_volume"]["status"] == "stale":
        return f"latest CME volume report is trade date {ctx['cme_volume']['latest_trade_date']}"
    if dep == "inventory" and ctx["inventory"].get("status") == "stale":
        return f"latest COMEX inventory report {ctx['inventory'].get('report_date')}"
    return None


def render_charts(conn, ctx, out_dir):
    """Run every chart job independently. One failure never stops the others."""
    manifest = []
    for spec in CHARTS:
        for win, window in spec["windows"]:
            fname = spec["file"].format(w=win)
            path = os.path.join(out_dir, fname)
            entry = {"family": spec["family"], "file": fname, "title": spec["title"], "window": win,
                     "window_label": _window_label(window), "email_title": spec["email"]}
            dep = "cme" if any(i.startswith("cme:") for i in spec["inputs"]) else (
                "inventory" if "inventory" in spec["inputs"] else None)
            try:
                dates = JOBS[spec["family"]](conn, ctx, spec, win, window, path)
                dates = pd.to_datetime(pd.Series(list(dates)))
                stale = _stale_reason(ctx, dep)
                entry.update({"status": "stale_input" if stale else "generated", "reason": stale,
                              "first_date": f"{dates.min():%Y-%m-%d}", "last_date": f"{dates.max():%Y-%m-%d}",
                              "points": int(len(dates)), "path": path})
            except MissingInput as e:
                entry.update({"status": "missing_input", "reason": str(e)})
            except Exception as e:  # render error: keep going
                entry.update({"status": "render_error", "reason": f"{type(e).__name__}: {e}",
                              "trace": traceback.format_exc()[-800:]})
                plt.close("all")
            manifest.append(entry)
    return manifest


def generate_dashboard_charts(*_args, **_kwargs):
    """Legacy entry point: re-render charts for the latest committed run (no network)."""
    from main_pipeline import replay
    return replay(None, deliver=False)


if __name__ == "__main__":
    generate_dashboard_charts()
