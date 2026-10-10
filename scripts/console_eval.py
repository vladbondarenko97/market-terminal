"""Grade the console engine: ask a RUNNING terminal a fixed set of questions and check each answer against the
terminal's own routes. Read-only: GET requests for the truth, POST /api/assistant/ask for the answers. Nothing is sent.

    .venv/bin/python scripts/console_eval.py --base http://127.0.0.1:8080
    .venv/bin/python scripts/console_eval.py --only chaos,news --reasoning low

The truth routes include live ones (/api/gex, /api/war_room, /api/scanner, /api/edges call Yahoo), and every question
is one or more calls to the local model, so a full pass takes a few minutes. Exit code 0 when every asked case
passes, 1 otherwise. 22 cases: 5 simple (answerable from the brief), 10 lookups, 7 multi-step.
"""
import argparse
import datetime as dt
import json
import re
import sys
import time
import uuid
import xml.etree.ElementTree as ET

import requests


def ask(base, question, model=None, reasoning=None):
    """One question in a fresh conversation -> {answer, tools, first, wall, error, lookups, tokens}."""
    body = {"question": question, "engine": "local", "conversation": str(uuid.uuid4())}
    body.update({k: v for k, v in (("model", model), ("reasoning", reasoning)) if v})
    t0, out = time.time(), {"answer": "", "tools": [], "first": None, "error": None}
    with requests.post(f"{base}/api/assistant/ask", json=body, stream=True, timeout=600) as r:
        for raw in r.iter_lines():
            if not raw or not raw.startswith(b"data:"):
                continue
            ev = json.loads(raw[5:])
            if ev["type"] == "delta":
                out["first"] = out["first"] or round(time.time() - t0, 1)
                out["answer"] += ev["text"]
            elif ev["type"] == "replace":
                out["answer"] = ev["text"]
            elif ev["type"] == "tool":
                out["tools"].append(ev["name"] + ("!" if ev.get("failed") else ""))
            elif ev["type"] == "error":
                out["error"] = ev["message"]
            elif ev["type"] == "done":
                out.update({k: ev.get(k) for k in ("lookups", "tokens", "model")})
    out["wall"] = round(time.time() - t0, 1)
    return out


def nums(text):
    return [float(x.replace(",", "")) for x in re.findall(r"-?\d[\d,]*\.?\d*", text.replace("−", "-")) if x.strip(",.-")]


def has_num(text, value, tol=0.006, abs_tol=0.011):
    return value is not None and any(abs(n - value) <= max(abs(value) * tol, abs_tol) for n in nums(text))


def has(text, *words):
    return all(w.lower() in text.lower() for w in words)


def any_of(text, *words):
    return any(w.lower() in text.lower() for w in words)


def find(d, *keys):
    """First numeric value under any of the candidate key names, searching nested dicts and lists."""
    stack = [d]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            for k in keys:
                if isinstance(o.get(k), (int, float)):
                    return float(o[k])
            stack += [v for v in o.values() if isinstance(v, (dict, list))]
        elif isinstance(o, list):
            stack += [v for v in o[:50] if isinstance(v, (dict, list))]
    return None


def truth(base):
    def get(path, **q):
        return requests.get(base + path, params=q, timeout=180)
    t = {"pos": get("/api/positions").json()["data"]}
    today = time.strftime("%Y-%m-%d")
    t["today"] = [r for r in t["pos"] if dt.datetime.fromisoformat(r["created_at"]).astimezone().date().isoformat() == today
                  and r["position_type"] != "CASH"]
    t["latest"] = t["pos"][0]
    gex = get("/api/gex", ticker="SPY").json()
    t["gex"] = gex.get("data", gex)
    t["war"], t["war2"] = get("/api/war_room").json(), get("/api/war_room", vix_shift_pct=100, oas_shift=2).json()
    t["fc"] = get("/api/forecast", ticker="SPY").json()
    t["scan"] = get("/api/scanner").json()
    t["gild"], t["spy_edges"] = get("/api/edges", symbol="GILD").json()["query"], get("/api/edges", symbol="SPY").json()["query"]
    r = get("/api/chaos")                                  # absent on a terminal older than card 14: those cases are skipped
    t["chaos"] = r.json().get("data") if r.status_code == 200 else None
    r = get("/api/macro_news")
    t["official"] = [a.get("title") for a in ET.fromstring(r.content).findall("article") if a.get("official") == "true"] if r.status_code == 200 else []
    return t


def cases(t):
    L, today, fc = t["latest"], t["today"], t["fc"]
    g = t["gex"]
    spot, cw, pw, zg = (find(g, *k) for k in (("spot", "spot_price", "price"), ("callWall", "call_wall"), ("putWall", "put_wall"), ("zeroGamma", "zero_gamma")))
    vmri, vmri2 = find(t["war"], "vmri", "score", "vmri_score"), find(t["war2"], "vmri", "score", "vmri_score")
    gild = next((r for r in t["scan"]["scanner"] if r["asset"] == "GILD" and "rsi2" in r), None)
    fired = [r["name"] for r in fc["signals"] if r["fired"]]
    tested_spy = [e["name"] for e in t["spy_edges"]["edges"] if e["edge"] == "tested"]
    g_rev = next(e for e in t["gild"]["edges"] if e["key"] == "reversal")
    calls = [r for r in t["pos"] if r["position_type"] == "CALL" and r.get("change_pct") is not None][:10]
    up = sum(r["change_pct"] > 0 for r in calls)
    two = sorted(today, key=lambda r: -(r.get("change_pct") or -1e9))
    nxt = (fc["data"].get("calendar") or {}).get("next") or {}
    day = lambda iso: str(int(iso[8:10])) if iso else "\0"
    clock = fc["run"]["generated_local"][11:16]
    cot = find((fc.get("positioning") or {}).get("silver") or {}, "cot_index")
    C = [
        ("S1 simple", "What was the system engine positions?", lambda a: has_num(a, L["strike"]) and has_num(a, L["entry_mid"]) and any_of(a, "call", "put", "cash")),
        ("S2 simple", "What's the latest engine position today and what was its entry price?", lambda a: has_num(a, L["strike"]) and has_num(a, L["entry_mid"])),
        ("S3 simple", "Is anything fired on Signal Watch right now?", lambda a: all(any_of(a, n.split(" (")[0], n.split()[0]) for n in fired)),
        ("S4 simple", "When did the pipeline last run?", lambda a: any_of(a, clock.lstrip("0"), clock)),
        ("S5 simple", "When are the next Fed decision and CPI release?", lambda a: has(a, day(nxt.get("fomc"))) and has(a, day(nxt.get("cpi")))),
        ("M1 lookup", "Where are the SPY gamma walls and zero gamma, and how far is spot from zero gamma in percent?",
         lambda a: has_num(a, cw) and has_num(a, pw) and has_num(a, zg, 0.002) and has_num(a, abs(spot / zg - 1) * 100, 0.2, 0.06)),
        ("M2 lookup", "What does the Day Scanner say about GILD right now?",
         lambda a: gild is not None and has_num(a, gild["trigger_price"], 0.004) and any_of(a, "waiting", "fired", "not fired", "wait")),
        ("M3 lookup", "What is the VMRI right now?", lambda a: has_num(a, vmri, 0.02, 0.06)),
        ("M4 lookup", "What is the latest engine ticket worth now versus its entry, in dollars and percent?",
         lambda a: has_num(a, L["entry_mid"]) and has_num(a, L["now_mid"], 0.03, 0.12) and has_num(a, L["change_pct"], 0.25, 1.6)),
        ("M5 lookup", "Which published edges have tested evidence on SPY?", lambda a: all(any_of(a, n.split(" (")[0], n.split()[0]) for n in tested_spy)),
        ("C1 complex", "Compare the engine's tickets from today: which has gained more in percent, and how many dollars is SPY from each strike right now?",
         lambda a: len(two) < 2 or (has_num(a, two[0]["strike"]) and has_num(a, two[1]["strike"])
                                    and has_num(a, abs(two[0]["underlying_now"] - two[0]["strike"]), 0.3, 0.35)
                                    and has_num(a, abs(two[1]["underlying_now"] - two[1]["strike"]), 0.3, 0.35))),
        ("C2 complex", "Is silver cheap or expensive versus its fair value, what is speculative positioning, and is any SLV signal close to firing?",
         lambda a: has_num(a, fc["silver_fair_value"]["fair_value"], 0.01) and (cot is None or has_num(a, cot, 0.04)) and any_of(a, "fair", "cheap", "expensive")),
        ("C3 complex", "If SPY fell 2% from here, would it be below zero gamma and below the put wall? Show the levels.",
         lambda a: has_num(a, spot * 0.98, 0.002) and has_num(a, zg, 0.002) and has_num(a, pw)),
        ("C4 complex", "What would VMRI be if VIX doubled and credit spreads widened by 2 points?", lambda a: has_num(a, vmri2, 0.02, 0.06)),
        ("C5 complex", "Give me a short midday brief: the engine ticket, what is fired, which Day Scanner ticker is closest to firing, and the next macro event.",
         lambda a: has_num(a, L["strike"]) and any_of(a, "CPI", "Fed", "expiry", "OPEX") and any_of(a, *[r["asset"] for r in t["scan"]["scanner"] if "rsi2" in r])),
        ("C6 complex", "Look at the engine's last 10 call tickets: how many are up versus their entry?",
         lambda a: any(abs(n - up) <= 1 for n in nums(a) if n == int(n) and 0 <= n <= 10)),
        ("C7 complex", "Run the Edge Lab on GILD and tell me whether there is a trade.",
         lambda a: any_of(a, "reversal", "dip") and (any_of(a, "active", "buy") if g_rev["active"] else any_of(a, "inactive", "no trade", "not active", "wait", "no "))),
    ]
    ch = t["chaos"]
    if ch and ch.get("score") is not None:                 # card 14 and the stored news section
        net = next((r for r in ch["levers"]["rows"] if r["key"] == "net_liquidity" and r["status"] == "fresh"), None)
        after = next((r for r in ch["event_study"]["rows"] if r["series"] == "S&P 500" and r["window"] == "+6..+60"), None)
        word = lambda label: re.split(r"[ /(]", label)[0]
        C += [
            ("N1 lookup chaos", "What is the Chaos/Peace score right now and which regime is it in?",
             lambda a: has_num(a, ch["score"], 0.03, 0.6) and has(a, ch["regime"])),
            ("N2 lookup chaos", "Which three readings are furthest from normal in the Chaos/Peace index, and how many of its components are missing?",
             lambda a: all(has(a, word(d["label"])) for d in ch["drivers"]) and has_num(a, len(ch["components_missing"]), 0, 0.01)),
            ("N3 lookup chaos", "What is net liquidity now and how much has it changed over the last four weeks?",
             lambda a: net is not None and has_num(a, net["level"], 0.002) and has_num(a, abs(net["change_20d"]), 0.03, 0.15)),
            ("N4 lookup chaos", "On average, what did the S&P 500 do in the 60 trading days after past midterm elections, how many midterms is that based on, and how strong is that evidence?",
             lambda a: after is not None and has_num(a, after["mean_pct"], 0.03, 0.06) and has_num(a, after["n"], 0, 0.01) and has(a, after["grade"])),
            ("N5 lookup news", "What are the latest headlines from official sources such as the Fed or the SEC?",
             lambda a: any(sum(w.lower() in a.lower() for w in big) >= max(2, round(0.6 * len(big)))
                           for title in t["official"][:8] if (big := [w for w in re.findall(r"[A-Za-z]{5,}", title)]))
             if t["official"] else any_of(a, "no headline", "none", "no official")),
        ]
    return C


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default="http://127.0.0.1:8080", help="address of the running terminal (default http://127.0.0.1:8080)")
    p.add_argument("--model", help="model name to ask for (default: the terminal's own)")
    p.add_argument("--reasoning", help="none | low | medium | high | auto (default: the terminal's setting)")
    p.add_argument("--only", help="comma-separated substrings of case names to run, e.g. simple,chaos or S3,N1")
    p.add_argument("--out", help="write every answer to this JSON file")
    a = p.parse_args()
    C = cases(truth(a.base.rstrip("/")))
    if a.only:
        C = [c for c in C if any(s.strip().lower() in c[0].lower() for s in a.only.split(","))]
    rows = []
    for name, q, check in C:
        r = ask(a.base.rstrip("/"), q, a.model, a.reasoning)
        try:
            ok = bool(check(r["answer"])) and not r["error"]
        except Exception as e:                             # a check that cannot be evaluated is a failed case, not a crash
            ok, r["error"] = False, f"check: {type(e).__name__}: {e}"
        rows.append((name, ok, r))
        print(f"  {'PASS' if ok else 'FAIL'} {name:16} {r['wall']:5.1f}s lookups {r.get('lookups')} {','.join(r['tools'])[:80]}"
              + (f" ERROR {str(r['error'])[:100]}" if r["error"] else ""), flush=True)
        if not ok:
            print("       > " + r["answer"].replace("\n", " ")[:420], flush=True)
    n, passed, secs = len(rows), sum(ok for _, ok, _ in rows), sorted(r["wall"] for _, _, r in rows)
    by = lambda k: f"{sum(ok for nm, ok, _ in rows if k in nm)}/{sum(k in nm for nm, _, _ in rows)}"
    print(f"== {passed}/{n} passed (simple {by('simple')}, lookup {by('lookup')}, complex {by('complex')}, chaos+news {sum(ok for nm, ok, _ in rows if nm[0] == 'N')}/{sum(nm[0] == 'N' for nm, _, _ in rows)})"
          + (f" | median {secs[n // 2]:.1f}s, slowest {secs[-1]:.1f}s, total {sum(secs):.0f}s" if n else ""))
    if a.out:
        json.dump([{"case": nm, "pass": ok, **r} for nm, ok, r in rows], open(a.out, "w"), indent=1)
    sys.exit(0 if n and passed == n else 1)
