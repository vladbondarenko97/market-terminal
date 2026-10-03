"""CME inventory/volume: format validation, parsing (offline, no credentials) and bounded acquisition."""
import io
import re
import time
import warnings
from datetime import datetime, date

import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

PARSER_VERSION_INVENTORY = "inventory_v2"
PARSER_VERSION_VOLUME = "volume_v2"

SILVER_STOCKS_URL = "https://www.cmegroup.com/delivery_reports/Silver_stocks.xls"
VOLUME_LATEST_URL = "https://www.cmegroup.com/ftp/daily_volume/daily_volume.xlsx"
VOLUME_ARCHIVE_URL = "https://www.cmegroup.com/ftp/daily_volume/daily_volume_{yyyymmdd}.xlsx"

TROY_OZ_PER_CONTRACT = {"SI": 5000, "SIL": 1000}

# Products used by the dashboard. Matched first by legacy description, then by code + F/O + side.
TARGET_PRODUCTS = {
    "ES_F": {"legacy": "E-MINI S&P 500 FUTURE", "code": "ES", "fo": "F", "side": None},
    "MES_F": {"legacy": "MICRO E-MINI S&P 500 FUTURES", "code": "MES", "fo": "F", "side": None},
    "SI_F": {"legacy": "SILVER FUTURES", "code": "SI", "fo": "F", "side": None},
    "SIL_F": {"legacy": "MICRO SILVER FUTURES", "code": "SIL", "fo": "F", "side": None},
    "ZN_F": {"legacy": "10Y NOTE FUTURE", "code": "21", "fo": "F", "side": None},
    "SO_C": {"legacy": "SILVER CALL", "code": "SO", "fo": "O", "side": "CALL"},
    "SO_P": {"legacy": "SILVER PUT", "code": "SO", "fo": "O", "side": "PUT"},
    "ES_C": {"legacy": "E-MINI S&P 500 CALL", "code": "ES", "fo": "O", "side": "CALL"},
    "ES_P": {"legacy": "E-MINI S&P 500 PUT", "code": "ES", "fo": "O", "side": "PUT"},
}
LEGACY_NAME = {k: v["legacy"] for k, v in TARGET_PRODUCTS.items()}


class CMEValidationError(ValueError):
    pass


# ---------------------------------------------------------------- format detection
def detect_format(data):
    if not data:
        return "empty"
    head = data[:2048]
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "xls"
    if head.startswith(b"PK\x03\x04"):
        return "xlsx" if b"xl/" in data[:65536] or b"[Content_Types].xml" in data[:65536] else "zip"
    lowered = head.lstrip().lower()
    if lowered.startswith(b"<!doctype html") or lowered.startswith(b"<html") or b"<html" in lowered[:1024]:
        return "html"
    if lowered.startswith(b"<?xml") or lowered.startswith(b"<"):
        return "xml_or_html"
    return "unknown"


def _read_sheets(data, fmt):
    engine = "xlrd" if fmt == "xls" else "openpyxl"
    try:
        return pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, engine=engine)
    except Exception as e:   # truncated/corrupt workbook: XLRDError, BadZipFile, ... → a rejection, not a crash
        raise CMEValidationError(f"unreadable {fmt} workbook: {type(e).__name__}: {e}") from e


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return None if pd.isna(v) else float(v)
    s = str(v).replace(",", "").strip()
    if s in ("", "nan", "-", "—"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_us_date(text):
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(text))
    if not m:
        return None
    return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))


# ---------------------------------------------------------------- inventory
INVENTORY_HEADERS = {
    "PREV TOTAL": "prev_total", "RECEIVED": "received", "WITHDRAWN": "withdrawn",
    "NET CHANGE": "net_change", "ADJUSTMENT": "adjustment", "TOTAL TODAY": "total_today",
}


def parse_inventory(data):
    """Parse a COMEX Silver_stocks workbook. Returns a dict with every depository row and the totals.
    Raises CMEValidationError when the bytes are not a valid inventory report."""
    fmt = detect_format(data)
    if fmt not in ("xls", "xlsx"):
        raise CMEValidationError(f"not a workbook (detected {fmt})")
    sheets = _read_sheets(data, fmt)
    name, df = next(iter(sheets.items()))
    cells = df.astype(object).where(pd.notna(df), None)

    report_date = activity_date = unit = metal = None
    header_row = None
    col_map = {}
    for i in range(len(cells)):
        row = [str(x).strip() if x is not None else "" for x in cells.iloc[i].tolist()]
        joined = " ".join(row).upper()
        for x in row:
            if x.upper().startswith("REPORT DATE"):
                report_date = _parse_us_date(x)
            elif x.upper().startswith("ACTIVITY DATE"):
                activity_date = _parse_us_date(x)
        if row[0].upper() in ("TROY OUNCE", "TROY OUNCES"):
            unit = "troy_oz"
        if row[0].upper() in ("SILVER", "GOLD", "COPPER", "PLATINUM", "PALLADIUM") and metal is None:
            metal = row[0].upper()
        if header_row is None and "DEPOSITORY" in joined and "TOTAL TODAY" in joined:
            header_row = i
            for j, x in enumerate(row):
                key = INVENTORY_HEADERS.get(x.upper())
                if key:
                    col_map[key] = j
    if header_row is None or "total_today" not in col_map:
        raise CMEValidationError("inventory header row (DEPOSITORY ... TOTAL TODAY) not found")
    if report_date is None:
        raise CMEValidationError("Report Date not found in workbook")

    depositories, totals = [], {}
    current = None
    for i in range(header_row + 1, len(cells)):
        label = cells.iloc[i, 0]
        label = str(label).strip() if label is not None else ""
        if not label:
            continue
        values = {k: _num(cells.iloc[i, j]) for k, j in col_map.items()}
        has_numbers = any(v is not None for v in values.values())
        upper = label.upper()
        if upper in ("TOTAL REGISTERED", "TOTAL ELIGIBLE", "COMBINED TOTAL"):
            totals[{"TOTAL REGISTERED": "registered", "TOTAL ELIGIBLE": "eligible",
                    "COMBINED TOTAL": "combined"}[upper]] = values
            continue
        if not has_numbers:
            if upper.startswith("THE INFORMATION") or "DISCLAIM" in upper or upper.startswith("THIS REPORT") \
                    or upper.startswith("FOR QUESTIONS"):
                continue
            current = {"depository": label, "rows": {}}
            depositories.append(current)
            continue
        if current is not None and upper in ("REGISTERED", "ELIGIBLE", "TOTAL"):
            current["rows"][upper.lower()] = values

    for k in ("registered", "eligible", "combined"):
        if k not in totals or totals[k].get("total_today") is None:
            raise CMEValidationError(f"total row for {k} missing")

    checks, warnings_ = [], []
    reg, elig, comb = (totals[k]["total_today"] for k in ("registered", "eligible", "combined"))
    diff = abs(reg + elig - comb)
    checks.append({"check": "registered+eligible=combined", "diff": diff, "ok": diff <= 1.0})
    for k, t in totals.items():
        if None not in (t.get("prev_total"), t.get("net_change"), t.get("adjustment"), t.get("total_today")):
            d = abs(t["prev_total"] + t["net_change"] + t["adjustment"] - t["total_today"])
            checks.append({"check": f"{k}: prev+net+adj=today", "diff": d, "ok": d <= 1.0})
    for c in checks:
        if not c["ok"]:
            warnings_.append(f"reconciliation failed: {c['check']} (diff {c['diff']:.3f})")

    return {
        "parser_version": PARSER_VERSION_INVENTORY, "format": fmt, "sheet": name, "metal": metal or "SILVER",
        "unit": unit or "troy_oz", "report_date": report_date.isoformat(),
        "activity_date": activity_date.isoformat() if activity_date else None,
        "totals": totals, "depositories": depositories, "checks": checks, "warnings": warnings_,
    }


def inventory_summary(parsed):
    """Legacy comex_inventory_history mapping. Changes are NET CHANGE (received - withdrawn);
    adjustments/reclassifications are reported separately."""
    t = parsed["totals"]
    return {
        "report_date": parsed["report_date"], "activity_date": parsed["activity_date"],
        "registered": t["registered"]["total_today"], "eligible": t["eligible"]["total_today"],
        "total": t["combined"]["total_today"],
        "reg_change": t["registered"].get("net_change"), "elig_change": t["eligible"].get("net_change"),
        "total_change": t["combined"].get("net_change"),
        "reg_adjustment": t["registered"].get("adjustment"), "elig_adjustment": t["eligible"].get("adjustment"),
        "total_adjustment": t["combined"].get("adjustment"),
        "reg_prev": t["registered"].get("prev_total"), "elig_prev": t["eligible"].get("prev_total"),
    }


# ---------------------------------------------------------------- volume
def _clean_header(x):
    return re.sub(r"\s+", " ", str(x).replace("\n", " ")).strip()


def parse_volume(data):
    """Parse a CME daily_volume workbook: trade date from inside the file, every row of every sheet."""
    fmt = detect_format(data)
    if fmt not in ("xlsx", "xls"):
        raise CMEValidationError(f"not a workbook (detected {fmt})")
    sheets = _read_sheets(data, fmt)
    trade_date = None
    report_version = "unspecified"
    out_sheets = {}
    for sname, df in sheets.items():
        cells = df.astype(object).where(pd.notna(df), None)
        header_row = None
        for i in range(min(len(cells), 15)):
            row = [str(x) if x is not None else "" for x in cells.iloc[i].tolist()]
            for x in row:
                if "TRADE DATE" in x.upper():
                    d = re.search(r"(\d{2})/(\d{2})/(\d{4})", x)
                    if d:
                        td = date(int(d.group(3)), int(d.group(1)), int(d.group(2)))
                        if trade_date and td != trade_date:
                            raise CMEValidationError(f"sheets disagree on trade date ({trade_date} vs {td})")
                        trade_date = td
                up = x.upper()
                if "PRELIMINARY" in up:
                    report_version = "preliminary"
                elif "FINAL" in up and report_version == "unspecified":
                    report_version = "final"
            if header_row is None and any(c.strip().upper() == "DESCRIPTION" for c in row):
                header_row = i
        if header_row is None:
            continue
        headers = [_clean_header(c) if c is not None else f"col{j}" for j, c in enumerate(cells.iloc[header_row])]
        body = cells.iloc[header_row + 1:].copy()
        body.columns = headers
        body = body.dropna(how="all")
        records = []
        for rec in body.to_dict(orient="records"):
            records.append({k: (v.item() if hasattr(v, "item") else v) for k, v in rec.items()})
        out_sheets[_clean_header(sname)] = {"headers": headers, "rows": records,
                                            "title": _clean_header(cells.iloc[0, 0]) if len(cells) else ""}
    if trade_date is None:
        raise CMEValidationError("Trade Date not found in workbook")
    by_product = next((s for n, s in out_sheets.items() if "BY PRODUCT" in n.upper()), None)
    if by_product is None:
        raise CMEValidationError("'Vol and OI by Product' sheet not found")
    need = {"Product Description", "Future/Option Indicator", "Total Volume"}
    if not need.issubset(set(by_product["headers"])):
        raise CMEValidationError(f"by-product sheet missing columns {need - set(by_product['headers'])}")
    return {"parser_version": PARSER_VERSION_VOLUME, "format": fmt, "trade_date": trade_date.isoformat(),
            "report_version": report_version, "sheets": out_sheets}


def _oi_key(headers):
    return next((h for h in headers if h.upper().startswith("OPEN INTEREST")), None)


def target_rows(parsed):
    """Return {product_key: {volume, open_interest, code, description, fo}} for dashboard products."""
    sheet = next(s for n, s in parsed["sheets"].items() if "BY PRODUCT" in n.upper())
    oi_col = _oi_key(sheet["headers"])
    found = {}
    for rec in sheet["rows"]:
        desc = str(rec.get("Product Description") or "").strip().upper()
        code = str(rec.get("Commodity Indicator") or "").strip().upper()
        if code.endswith(".0"):
            code = code[:-2]
        fo = str(rec.get("Future/Option Indicator") or "").strip().upper()
        for key, spec in TARGET_PRODUCTS.items():
            if key in found:
                continue
            match = desc == spec["legacy"]
            if not match and code == spec["code"] and fo == spec["fo"]:
                if spec["side"] is None:
                    match = "FUTURE" in desc and "MICRO" not in desc if key in ("ES_F", "SI_F") else True
                else:
                    match = desc.endswith(spec["side"]) and "WEEK" not in desc and "EOM" not in desc
            if match:
                found[key] = {"volume": _num(rec.get("Total Volume")),
                              "open_interest": _num(rec.get(oi_col)) if oi_col else None,
                              "code": code, "description": desc, "fo": fo}
    return found


# ---------------------------------------------------------------- acquisition (bounded)
class AcquisitionResult:
    def __init__(self, outcome, data=None, http_status=None, detail=None, url=None, attempts=0):
        self.outcome = outcome        # ok | access_denied | unavailable | rate_limited | rejected | error | skipped
        self.data = data
        self.http_status = http_status
        self.detail = detail
        self.url = url
        self.attempts = attempts

    def __repr__(self):
        return f"<AcquisitionResult {self.outcome} http={self.http_status} {self.detail or ''}>"


def fetch_bounded(url, *, validate, max_attempts=2, cooldown=20, timeout=30, http_get=None, sleep=time.sleep):
    """One source, bounded attempts. 401/403 stop immediately; 429 gets one bounded cooldown;
    404 means unavailable; HTML/login pages are rejected before they can replace an archive."""
    if http_get is None:
        import requests
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                                 "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
                   "Accept": "application/vnd.ms-excel,application/vnd.openxmlformats-officedocument."
                             "spreadsheetml.sheet,*/*"}

        def http_get(u):
            return requests.get(u, headers=headers, timeout=timeout)
    attempts = 0
    last = None
    while attempts < max_attempts:
        attempts += 1
        try:
            resp = http_get(url)
        except Exception as e:  # network failure
            last = AcquisitionResult("error", detail=f"{type(e).__name__}: {e}", url=url, attempts=attempts)
            continue
        status = resp.status_code
        if status in (401, 403):
            return AcquisitionResult("access_denied", http_status=status, url=url, attempts=attempts,
                                     detail="access refused; no further attempts for this source this run")
        if status == 404:
            return AcquisitionResult("unavailable", http_status=status, url=url, attempts=attempts)
        if status == 429:
            last = AcquisitionResult("rate_limited", http_status=status, url=url, attempts=attempts)
            if attempts < max_attempts:
                retry_after = resp.headers.get("Retry-After", "")
                wait = min(cooldown, int(retry_after)) if retry_after.isdigit() else cooldown
                sleep(wait)
            continue
        if status != 200:
            last = AcquisitionResult("error", http_status=status, url=url, attempts=attempts)
            continue
        data = resp.content
        try:
            validate(data)
        except CMEValidationError as e:
            return AcquisitionResult("rejected", data=data, http_status=status, url=url, attempts=attempts,
                                     detail=str(e))
        return AcquisitionResult("ok", data=data, http_status=status, url=url, attempts=attempts)
    return last or AcquisitionResult("error", detail="no attempt made", url=url, attempts=attempts)


def browser_fetch(url, state_file=None, *, timeout_ms=30000, headless=False, profile_dir=None):
    """Browser adapter (the legacy acquisition path): one bounded attempt, never prompts for login/MFA.
    A saved session (state.json) is used when present. The session file itself is never captured."""
    import os
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            if profile_dir:
                context = open_persistent_context(p, profile_dir, state_file)
                browser = context
            else:
                browser = p.chromium.launch(headless=headless, args=["--disable-http2",
                                                                     "--disable-blink-features=AutomationControlled"])
                kw = {"accept_downloads": True, "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"}
                if state_file and os.path.exists(state_file):
                    kw["storage_state"] = state_file
                context = browser.new_context(**kw)
            page = context.new_page()
            try:
                with page.expect_download(timeout=timeout_ms) as info:
                    try:
                        page.goto(url)
                    except Exception as e:
                        if "Download is starting" not in str(e):
                            raise
                path = info.value.path()
                with open(path, "rb") as f:
                    data = f.read()
            finally:
                browser.close()
        return AcquisitionResult("ok", data=data, url=url, attempts=1)
    except Exception as e:
        return AcquisitionResult("error", url=url, detail=f"browser: {e}", attempts=1)


# ---------------------------------------------------------------- logged-in CME FTP directory (volume)
CME_LOGIN_URL = "https://login.cmegroup.com/sso/accountstatus/showAuth.action"
VOLUME_DIR_URL = "https://www.cmegroup.com/ftp/daily_volume/"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/121.0.0.0 Safari/537.36")
_ARGS = ["--disable-http2", "--disable-blink-features=AutomationControlled"]


def open_persistent_context(p, profile_dir, state_file=None):
    """Persistent Chromium profile: cookies, local storage, IndexedDB and Duo 'remember me' data survive
    between runs so the CME login lasts as long as CME allows. Seeded once from state.json if the profile is new."""
    import json
    import os
    new_profile = not os.path.exists(os.path.join(profile_dir, "Default"))
    os.makedirs(profile_dir, exist_ok=True)
    ctx = p.chromium.launch_persistent_context(str(profile_dir), headless=False, args=_ARGS, user_agent=_UA,
                                               accept_downloads=True)
    if new_profile and state_file and os.path.exists(state_file):
        try:
            with open(state_file) as f:
                cookies = json.load(f).get("cookies", [])
            if cookies:
                ctx.add_cookies(cookies)
        except Exception:
            pass
    return ctx


def save_session(ctx, state_file):
    """Keep a portable copy of the cookies (backup / other machines). Never captured into the data lake."""
    if state_file:
        try:
            ctx.storage_state(path=str(state_file))
        except Exception:
            pass


def _looks_logged_in(url):
    if "www.cmegroup.com" in url:
        return True
    return "login.cmegroup.com" in url and "showAuth" not in url and not url.rstrip("/").lower().endswith("login")


def wait_for_login(ctx, *, username="", password="", wait_seconds=900, poll_seconds=5, notify=None):
    """Open the CME login page in the persistent browser, pre-fill credentials and pause (bounded) until the
    operator finishes login + MFA. Returns True if a logged-in page was reached."""
    import time as _t
    page = ctx.new_page()
    page.goto(CME_LOGIN_URL)
    try:
        page.bring_to_front()
    except Exception:
        pass
    if _looks_logged_in(page.url):
        page.close()
        return True
    try:
        if username:
            page.locator('input[type="text"], input[type="email"]').first.fill(username)
        if password:
            page.locator('input[type="password"]').first.fill(password)
    except Exception:
        pass
    msg = f"CME login needed: finish the login + MFA in the browser window (waiting up to {wait_seconds // 60} min)."
    print(f"👉 {msg}", flush=True)
    if notify:
        try:
            notify(msg)
        except Exception:
            pass
    deadline = _t.monotonic() + wait_seconds
    while _t.monotonic() < deadline:
        try:
            if _looks_logged_in(page.url):
                page.wait_for_timeout(1500)
                page.close()
                return True
            page.wait_for_timeout(poll_seconds * 1000)
        except Exception:
            return False   # window closed
    try:
        page.close()
    except Exception:
        pass
    return False


def interactive_login(state_file, username="", password="", *, profile_dir=None, wait_seconds=900):
    """`python main_pipeline.py cme-login`: log in once (MFA) and verify a workbook download."""
    import os
    from playwright.sync_api import sync_playwright
    profile_dir = profile_dir or os.path.join(os.path.dirname(state_file), ".cme_browser_profile")
    with sync_playwright() as p:
        ctx = open_persistent_context(p, profile_dir, state_file)
        try:
            ok = wait_for_login(ctx, username=username, password=password, wait_seconds=wait_seconds)
            if not ok:
                return False
            save_session(ctx, state_file)
            page = ctx.new_page()
            page.goto(VOLUME_DIR_URL)
            page.wait_for_timeout(2500)
            probe = _newest_listing_file(page)
            if not probe:
                return False
            code, body = _download(page, probe)
            print(f"probe {probe.rsplit('/', 1)[-1]}: HTTP {code}", flush=True)
            return body is not None and detect_format(body) in ("xlsx", "xls")
        finally:
            save_session(ctx, state_file)
            ctx.close()


def _listing_files(page):
    hrefs = page.eval_on_selector_all("a", "els => els.map(e => e.href)")
    out = {}
    for h in hrefs:
        m = re.search(r"/daily_volume_(\d{8})\.xlsx$", h)
        if m:
            out[m.group(1)] = h
    return out


def _newest_listing_file(page):
    files = _listing_files(page)
    return files[max(files)] if files else None


def _download(page, url, timeout_ms=30000):
    """Navigate like a user (CME refuses side-channel requests). Returns (http_status, bytes_or_None)."""
    status = {}

    def on_resp(r):
        if r.url == url:
            status.setdefault("code", r.status)
    page.on("response", on_resp)
    try:
        with page.expect_download(timeout=timeout_ms) as dl:
            try:
                page.goto(url, referer=VOLUME_DIR_URL)
            except Exception as e:
                if "Download is starting" not in str(e):
                    raise
        with open(dl.value.path(), "rb") as f:
            return status.get("code", 200), f.read()
    except Exception:
        return status.get("code"), None
    finally:
        page.remove_listener("response", on_resp)


def browser_fetch_volume_listing(state_file, have_dates, *, max_files=10, earliest=None, profile_dir=None,
                                 login_wait_seconds=900, username="", password="", notify=None):
    """Read the FTP listing in the persistent logged-in browser and download the newest files whose trade dates
    are not in the lake (bounded). If CME refuses (login error), pause once for the operator to log in, then
    retry. Returns (results: list of (yyyymmdd, AcquisitionResult), listing_info)."""
    import os
    from playwright.sync_api import sync_playwright
    profile_dir = profile_dir or os.path.join(os.path.dirname(state_file), ".cme_browser_profile")
    results = []
    info = {"outcome": "error"}
    with sync_playwright() as p:
        ctx = open_persistent_context(p, profile_dir, state_file)
        try:
            page = ctx.new_page()
            page.goto(VOLUME_DIR_URL, timeout=30000)
            page.wait_for_timeout(2000)
            files = _listing_files(page)
            if not files:
                return [], {"outcome": "unavailable", "detail": "FTP listing had no daily_volume files"}
            wanted = sorted((d for d in files if d not in have_dates and (earliest is None or d >= earliest)),
                            reverse=True)[:max_files]
            info = {"outcome": "ok", "listing_latest": max(files), "requested": wanted, "login_prompted": False}
            pending = list(wanted)
            while pending:
                d = pending[0]
                code, body = _download(page, files[d])
                if body is None and code in (400, 401, 403):
                    if info["login_prompted"] or login_wait_seconds <= 0:
                        results.append((d, AcquisitionResult(
                            "login_required", http_status=code, url=files[d], attempts=1,
                            detail="CME refused the download (login needed); run `python main_pipeline.py cme-login`")))
                        break
                    info["login_prompted"] = True
                    if not wait_for_login(ctx, username=username, password=password,
                                          wait_seconds=login_wait_seconds, notify=notify):
                        results.append((d, AcquisitionResult("login_required", http_status=code, url=files[d],
                                                             attempts=1, detail="login not completed in time")))
                        break
                    save_session(ctx, state_file)
                    page.goto(VOLUME_DIR_URL)
                    page.wait_for_timeout(1500)
                    continue            # retry the same file once after login
                pending.pop(0)
                if body is None:
                    results.append((d, AcquisitionResult("error", http_status=code, url=files[d], attempts=1,
                                                         detail="download did not start")))
                    break
                try:
                    parse_volume(body)
                except CMEValidationError as e:
                    results.append((d, AcquisitionResult("rejected", data=body, http_status=code, url=files[d],
                                                         detail=str(e), attempts=1)))
                    continue
                results.append((d, AcquisitionResult("ok", data=body, http_status=code, url=files[d], attempts=1)))
            return results, info
        except Exception as e:
            return results, {**info, "outcome": "error", "detail": f"browser: {e}"}
        finally:
            save_session(ctx, state_file)
            ctx.close()
