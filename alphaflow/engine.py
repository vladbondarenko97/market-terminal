import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import databento as db
import pandas as pd
import yfinance as yf

# The project's settings live in config.py one folder up (it loads .env, honours the DB_API_KEY alias).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

DB_PATH = os.path.join(os.path.dirname(__file__), "alphaflow.db")
CACHE_FILE = os.path.join(os.path.dirname(__file__), "small_caps.csv")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Create only: stored results survive a restart. A scan replaces them once it has fresh ones (see run_historical_scan).
    c.execute('''CREATE TABLE IF NOT EXISTS swing_plays
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  date TEXT,
                  time_of_sweep TEXT,
                  ticker TEXT,
                  contract TEXT,
                  volume INTEGER,
                  oi INTEGER,
                  spend REAL,
                  term TEXT,
                  expiration TEXT,
                  entry TEXT,
                  max_risk REAL)''')
    conn.commit()
    conn.close()

init_db()

def get_cached_universe():
    if not os.path.exists(CACHE_FILE):
        # Fallback if the user hasn't run cache_universe.py yet
        print("small_caps.csv not found! Using fallback demo list. Run cache_universe.py for the full scan.")
        return pd.DataFrame([
            {"Ticker": "SPXS", "Total_OI_Approx": 5000},
            {"Ticker": "RIG", "Total_OI_Approx": 10000},
            {"Ticker": "PLUG", "Total_OI_Approx": 15000}
        ])
    df = pd.read_csv(CACHE_FILE)
    return df

def fetch_actual_databento(symbols, date):
    """OPRA trades for `symbols` on `date`. An empty frame means the query worked and found nothing; a missing
    key or a failed request raises RuntimeError so the caller does not mistake it for 'no sweeps'."""
    api_key = config.DATABENTO_API_KEY
    if not api_key or "YOUR" in api_key:
        raise RuntimeError("DATABENTO_API_KEY is not set in .env, so no scan was run")

    client = db.Historical(key=api_key)

    # OPRA parent symbology requires the .OPT suffix (e.g., SPXS.OPT)
    formatted_symbols = [f"{sym}.OPT" for sym in symbols]

    print(f"📡 Querying Databento OPRA (tcbbo) for {len(formatted_symbols)} parent symbols on {date}...")

    try:
        data = client.timeseries.get_range(
            dataset='OPRA.PILLAR',
            schema='tcbbo',
            symbols=formatted_symbols,
            stype_in='parent',
            start=date,
            end=f"{date}T23:59:00"
        )
        return data.to_df()
    except Exception as e:
        print(f"Databento API error/limit reached: {e}")
        raise RuntimeError(f"Databento request failed ({type(e).__name__}); the stored results were kept") from e

def get_exact_strike_oi(ticker, opra_symbol):
    """
    Lazy Evaluation: Fetches exact Open Interest for a specific option contract 
    from Yahoo Finance on-the-fly. Returns None when it is unknown (never a stand-in value).
    """
    try:
        match = re.search(r'(\d{6})([CP])(\d{8})', str(opra_symbol))
        if not match:
            return None
            
        yymmdd = match.group(1)
        opt_type = match.group(2)
        strike_str = match.group(3)
        
        expiry_date = f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
        strike_val = float(strike_str) / 1000.0
        
        tkr = yf.Ticker(ticker)
        chain = tkr.option_chain(expiry_date)
        options_df = chain.calls if opt_type == 'C' else chain.puts
        
        row = options_df[options_df['strike'] == strike_val]
        if not row.empty:
            oi = row['openInterest'].values[0]
            return int(oi) if pd.notna(oi) else None
    except Exception as e:
        print(f"Lazy OI fetch failed for {opra_symbol}: {e}")
    return None

def run_historical_scan(params):
    # Dynamic Date from UI
    target_date = params.get("scan_date", (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d'))
    
    print(f"🔍 Starting Historical Scan for {target_date}...")
    min_spend = float(params.get("min_spend", 50000))
    min_vol_oi = float(params.get("vol_oi", 1.5))
    
    universe_df = get_cached_universe()
    symbols = universe_df['Ticker'].tolist()
    
    df = fetch_actual_databento(symbols, target_date)
    
    detected = []
    
    if not df.empty:
        has_ask = 'ask_px_00' in df.columns
        if not has_ask:
            print("Warning: 'ask_px_00' not found. Proceeding without strict ask filter.")
            
        if 'instrument_class' in df.columns:
            calls = df[df['instrument_class'] == 'C'].copy()
        else:
            calls = df.copy()
            
        if not calls.empty:
            if has_ask:
                aggressive = calls[calls['price'] >= calls['ask_px_00']].copy()
            else:
                aggressive = calls.copy()
            
            aggressive['premium'] = aggressive['price'] * aggressive['size'] * 100
            
            if not isinstance(aggressive.index, pd.DatetimeIndex):
                aggressive.index = pd.to_datetime(aggressive.index)
            
            for contract, group in aggressive.groupby('symbol'):
                ticker = "".join([c for c in str(contract).split('2')[0] if c.isalpha()])
                
                rolling_15m = group.resample('15Min').sum(numeric_only=True)
                
                for time_window, row in rolling_15m.iterrows():
                    total_vol = row['size']
                    total_spend = row['premium']
                    
                    # 1. Baseline noise filter (Spend > $50k and Baseline Volume)
                    if total_spend >= min_spend and total_vol >= 100:
                        # 2. LAZY EVALUATION: Only fetch exact OI if it passes the massive baseline
                        exact_oi = get_exact_strike_oi(ticker, contract)
                        
                        # 3. Final validation against exact strike OI. Unknown OI cannot be validated, so the
                        #    window is skipped; OI 0 (a brand-new strike) passes any volume/OI threshold.
                        if exact_oi is None:
                            continue
                        if exact_oi == 0 or (total_vol / exact_oi) >= min_vol_oi:
                            dte = 30 # Default assumption
                            rec_expiry = (datetime.strptime(target_date, '%Y-%m-%d') + timedelta(days=dte + 14)).strftime('%Y-%m-%d')
                            
                            detected.append({
                                "date": target_date,
                                "time_of_sweep": time_window.strftime('%H:%M:%S'),
                                "ticker": ticker,
                                "contract": contract,
                                "volume": int(total_vol),
                                "oi": exact_oi,
                                "spend": float(total_spend),
                                "term": "Mid Term",
                                "expiration": rec_expiry,
                                "entry": "NEXT OPEN",
                                "max_risk": 500.0
                            })
    else:
        print(f"Databento returned no trades for {target_date} (weekend, holiday or no activity in the universe).")
    
    # Only a scan whose Databento query succeeded gets here (a failed one raised above), so the stored results
    # are replaced by the latest scan's, including "nothing found". Rows from earlier scans are not kept.
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM swing_plays")
    
    for p in detected:
        c.execute('''INSERT INTO swing_plays (date, time_of_sweep, ticker, contract, volume, oi, spend, term, expiration, entry, max_risk)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                  (p['date'], p['time_of_sweep'], p['ticker'], p['contract'], p['volume'], p['oi'], p['spend'], p['term'], p['expiration'], p['entry'], p['max_risk']))
    conn.commit()
    conn.close()
    
    return detected

def get_latest_results():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM swing_plays ORDER BY spend DESC")
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]
