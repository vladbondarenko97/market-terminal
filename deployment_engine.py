import json
import os
import math
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from config import DATA_DIR, PROJECT_ROOT

# --- CONSTANTS & CONFIG ---
DEPLOYMENT_STATE_FILE = os.path.join(PROJECT_ROOT, "deployment_state.json")
DEPLOYMENT_PAYLOAD_FILE = os.path.join(PROJECT_ROOT, "deployment_payload.json")
MACRO_LEDGER_FILE = os.path.join(DATA_DIR, "macro_master_ledger.csv")

# Kelly Criterion Assumptions (operator choices, reported as assumptions in the payload, not market data)
WIN_RATE = 0.75 # Assumed 75% win rate for Put Credit Spreads
RISK_REWARD = 1.0 # Assumed 1:1 risk/reward

# Physical silver eagles count as a buy when their premium over spot is below this many percent.
PREMIUM_BUY_BELOW_PCT = 15.0

# A value this script cannot read or compute is None in the payload, with the reason under payload["missing"].
# Nothing is filled in with a stand-in number.

def get_latest_vmri():
    """The most recent VMRI score from the macro master ledger as (score, reason). The score is None, with the
    reason, when the ledger or a score in it is not there."""
    if not os.path.exists(MACRO_LEDGER_FILE):
        return None, "macro_master_ledger.csv not found in the data folder"
    try:
        df = pd.read_csv(MACRO_LEDGER_FILE)
    except Exception as e:
        return None, f"macro_master_ledger.csv could not be read ({type(e).__name__})"
    if 'VMRI_Score' not in df.columns:
        return None, "macro_master_ledger.csv has no VMRI_Score column"
    valid_scores = df['VMRI_Score'].dropna()
    if valid_scores.empty:
        return None, "macro_master_ledger.csv has no VMRI_Score values"
    return float(valid_scores.iloc[-1]), None

def calculate_volatility(ticker_symbol):
    """Calculates 30-day Historical Volatility (HV) and pulls current Implied Volatility (IV)."""
    try:
        ticker = yf.Ticker(ticker_symbol)
        
        # 1. Historical Volatility (30 days)
        hist = ticker.history(period="3mo")
        if hist.empty or len(hist) < 30:
            return None, None
            
        hist['returns'] = np.log(hist['Close'] / hist['Close'].shift(1))
        hv = hist['returns'].tail(30).std() * np.sqrt(252) * 100 # Annualized percentage
        if math.isnan(hv):
            return None, None
        
        # 2. Implied Volatility
        iv = None
        if hasattr(ticker, 'options') and len(ticker.options) > 0:
            # Find the nearest expiration at least 7 days out
            today = datetime.today()
            target_exp = None
            for exp in ticker.options:
                exp_date = datetime.strptime(exp, "%Y-%m-%d")
                if (exp_date - today).days >= 7:
                    target_exp = exp
                    break
            
            if not target_exp:
                target_exp = ticker.options[0]
                
            opt_chain = ticker.option_chain(target_exp)
            current_price = hist['Close'].iloc[-1]
            
            # Find closest At-The-Money (ATM) Put
            puts = opt_chain.puts
            if not puts.empty:
                atm_put = puts.iloc[(puts['strike'] - current_price).abs().argsort()[:1]]
                iv = atm_put['impliedVolatility'].values[0] * 100
                if math.isnan(iv):
                    iv = None
        
        return round(hv, 2), round(iv, 2) if iv is not None else None
    except Exception as e:
        print(f"Error calculating vol for {ticker_symbol}: {e}")
        return None, None

def check_physical_arbitrage():
    """The physical silver eagle premium over spot from the first listing in the latest tactical_ruling.txt, and
    the buy signal it gives. A part that cannot be read is None with its reason in "missing"; there is no
    assumed premium."""
    result = {"premium_pct": None, "signal": None, "missing": {}}

    import glob
    import xml.etree.ElementTree as ET

    # Offline runs write into offline_<run_id> folders; their reports are not real market data.
    tactical_files = [f for f in glob.glob(os.path.join(DATA_DIR, "**", "tactical_ruling.txt"), recursive=True)
                      if not any(part.startswith("offline_") for part in os.path.normpath(f).split(os.sep))]
    if not tactical_files:
        result["missing"]["physical_premium"] = "no tactical_ruling.txt in the data folder"
        return result
    try:
        latest_file = max(tactical_files, key=os.path.getmtime)
        phys_arb = ET.parse(latest_file).getroot().find('.//physical_arbitrage')
        first_listing = phys_arb.find('listing') if phys_arb is not None else None
        if first_listing is None:
            result["missing"]["physical_premium"] = "the latest tactical_ruling.txt has no physical eagle listing"
            return result
        prem_str = first_listing.attrib.get('premium_percent')
        if prem_str is None:
            result["missing"]["physical_premium"] = "the first eagle listing has no premium_percent"
            return result
        try:
            premium_pct = float(prem_str.replace('%', ''))
        except ValueError:
            result["missing"]["physical_premium"] = f"the first eagle listing's premium_percent is not a number ({prem_str})"
            return result
    except Exception as e:
        result["missing"]["physical_premium"] = f"tactical_ruling.txt could not be read ({type(e).__name__})"
        return result
    result["premium_pct"] = premium_pct
    result["signal"] = "BUY SIGNAL" if premium_pct < PREMIUM_BUY_BELOW_PCT else "HOLD"
    return result

def calculate_kelly(win_rate, risk_reward):
    """Calculates the Kelly percentage."""
    # K = W - [(1 - W) / R]
    k = win_rate - ((1 - win_rate) / risk_reward)
    return max(0, k) # Prevent negative allocations

def load_state():
    """The operator's deployment_state.json as (state, reason). With no readable file the state is {} and the
    reason says why; balances and active trades are then missing, not guessed.

    Expected shape: {"bucket_A_base_layer": {"balance": N}, "bucket_B_velocity_engine": {"balance": N},
    "active_trades": [{"trade": "...", "status": "...", "current_ev": "...", "capital_at_risk": N}, ...]}"""
    try:
        with open(DEPLOYMENT_STATE_FILE, "r") as f:
            state = json.load(f)
    except FileNotFoundError:
        return {}, "deployment_state.json not found in the repository root"
    except (OSError, json.JSONDecodeError) as e:
        return {}, f"deployment_state.json could not be read ({type(e).__name__})"
    if not isinstance(state, dict):
        return {}, "deployment_state.json is not a JSON object"
    return state, None


def read_balance(state, key, label, missing):
    """state[key]["balance"] when it is a number, else None with a reason under missing[label]."""
    bucket = state.get(key)
    value = bucket.get("balance") if isinstance(bucket, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    missing.setdefault(label, f"deployment_state.json has no number at {key}.balance")
    return None


def active_trades_from(state, missing):
    """(trades, capital_at_risk) as the operator recorded them: the trades' own names, status and EV text, and the
    sum of their capital_at_risk. This script does not price trades, so nothing is computed or made up. A part the
    state does not give is None with a reason under missing."""
    trades = state.get("active_trades")
    if not isinstance(trades, list):
        missing.setdefault("active_trades", "deployment_state.json has no active_trades list")
        missing.setdefault("capital_at_risk", "the active trades are unknown")
        return None, None
    rows = []
    for t in trades:
        t = t if isinstance(t, dict) else {"trade": str(t)}
        rows.append({
            "trade": t.get("trade") or t.get("name"),
            "status": t.get("status"),
            "current_ev": t.get("current_ev"),
            "capital_at_risk": t.get("capital_at_risk"),
        })
    amounts = [r["capital_at_risk"] for r in rows]
    if all(isinstance(a, (int, float)) and not isinstance(a, bool) for a in amounts):
        return rows, round(sum(amounts), 2)
    missing.setdefault("capital_at_risk", "an active trade in deployment_state.json has no capital_at_risk number")
    return rows, None


def run_engine():
    missing = {}

    # --- PHASE 1: RECON ENGINE ---
    print("Running Recon Engine...")
    vmri, vmri_reason = get_latest_vmri()
    if vmri is None:
        missing["vmri"] = vmri_reason

    tickers = {"SPY": "SPY", "SLV": "SLV", "USO": "USO", "ES": "ES=F"}
    vol_data = {}

    for name, sym in tickers.items():
        hv, iv = calculate_volatility(sym)
        vol_data[name] = {"HV": hv, "IV": iv}
        if hv is None or iv is None:
            missing[f"volatility_{name}"] = f"{sym}: historical or implied volatility unavailable from Yahoo Finance"

    arb_data = check_physical_arbitrage()
    missing.update(arb_data.pop("missing"))

    # Identify Setups
    setups = []

    # 1. Volatility Skew Setups (Put Credit Spreads when IV > HV)
    for name, data in vol_data.items():
        if data["HV"] is not None and data["IV"] is not None and data["IV"] > data["HV"]:
            edge = data["IV"] - data["HV"]
            setups.append({
                "type": "Volatility Skew",
                "asset": name,
                "description": f"IV ({data['IV']}%) > HV ({data['HV']}%) by {edge:.2f}%. Put Credit Spread optimal.",
                "edge_score": edge
            })

    # 2. Arbitrage Setups
    if arb_data["premium_pct"] is not None and 0 < arb_data["premium_pct"] < PREMIUM_BUY_BELOW_PCT:
        setups.append({
            "type": "Physical Arbitrage",
            "asset": "Silver Eagles",
            "description": f"Physical premium is at {arb_data['premium_pct']}%. Hard-asset buy signal.",
            "edge_score": PREMIUM_BUY_BELOW_PCT - arb_data["premium_pct"] # Arbitrary edge scoring for ranking
        })

    # Sort top 3 setups by edge score
    setups = sorted(setups, key=lambda x: x["edge_score"], reverse=True)[:3]

    # --- PHASE 2 & 3: KELLY ENGINE & STATE MANAGEMENT ---
    print("Running Kelly Engine & Sizing...")
    state, state_reason = load_state()
    if state_reason:
        missing["state"] = state_reason

    bucket_a = read_balance(state, "bucket_A_base_layer", "bucket_A_total", missing)
    total_bankroll = read_balance(state, "bucket_B_velocity_engine", "bucket_B_total", missing)
    kelly_pct = calculate_kelly(WIN_RATE, RISK_REWARD)

    # The Half-Kelly Constraint (Max 5%)
    adjusted_kelly_pct = min(kelly_pct / 2, 0.05)

    capital_per_trade = total_bankroll * adjusted_kelly_pct if total_bankroll is not None else None
    if capital_per_trade is None:
        missing["suggested_capital"] = "bucket B balance unknown, so no trade can be sized"

    # Assign sizing to the setups
    for setup in setups:
        setup["suggested_allocation_pct"] = round(adjusted_kelly_pct * 100, 2)
        setup["suggested_capital"] = round(capital_per_trade, 2) if capital_per_trade is not None else None

    active_trades, active_capital_at_risk = active_trades_from(state, missing)

    # --- ASSEMBLE PAYLOAD ---
    payload = {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "vmri": round(vmri, 2) if vmri is not None else None,
        "liquidity": {
            "bucket_A_total": bucket_a,
            "bucket_B_total": total_bankroll,
            "capital_at_risk": active_capital_at_risk
        },
        "kelly_metrics": {
            "win_rate_assumption": WIN_RATE,
            "risk_reward_assumption": RISK_REWARD,
            "full_kelly_pct": round(kelly_pct * 100, 2),
            "half_kelly_cap_pct": round(adjusted_kelly_pct * 100, 2)
        },
        "scanner_output": setups,
        "active_trades_ev": active_trades,
        "missing": missing,
    }

    with open(DEPLOYMENT_PAYLOAD_FILE, "w") as f:
        json.dump(payload, f, indent=4)

    js_payload_file = DEPLOYMENT_PAYLOAD_FILE.replace('.json', '.js')
    with open(js_payload_file, "w") as f:
        f.write(f"const DASHBOARD_PAYLOAD = {json.dumps(payload, indent=4)};")

    print(f"Successfully generated payload at {DEPLOYMENT_PAYLOAD_FILE} and {js_payload_file}")
    for field, reason in missing.items():
        print(f"  missing {field}: {reason}")

if __name__ == "__main__":
    run_engine()
