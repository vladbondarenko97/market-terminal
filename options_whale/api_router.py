import sys
import pandas as pd
from flask import Flask, jsonify, render_template_string, Response, request, render_template
from flask_cors import CORS # Add this
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET
from xml.dom import minidom
import requests
import warnings
import subprocess
import os
import re
import random
import math
import time
import databento as db
import numpy as np

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from config import required_env, DATA_DIR as _DATA_DIR, PROJECT_ROOT as _PROJECT_ROOT
from scipy.stats import norm
from playwright.sync_api import sync_playwright
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from quant_engine import QuantEngine


PYTHON_BIN = "/usr/local/Caskroom/miniconda/base/bin/python3"
RUN_COMMAND = str(_PROJECT_ROOT / "run_dashboard.command")

OPTIONS_SCRIPT = str(_PROJECT_ROOT / "options_scanner.py")
EBAY_SCRIPT_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'ebay.py'))

INVENTORY_CSV = str(_DATA_DIR / "comex_inventory_history.csv")
LEDGER_CSV = str(_DATA_DIR / "macro_master_ledger.csv")

DATA_DIR = str(_DATA_DIR)
LEDGER_FILE = os.path.join(DATA_DIR, "physical_arbitrage_ledger.csv")

analyzer = SentimentIntensityAnalyzer()
quant = QuantEngine(LEDGER_CSV)

# Suppress pandas FutureWarnings for clean terminal output
warnings.simplefilter(action='ignore', category=FutureWarning)

# ==========================================
# IN-MEMORY CACHE MANAGER
# ==========================================
class DataCache:
    def __init__(self, ttl_seconds=60):
        self.ttl = ttl_seconds
        self.cache = {}
        self.last_update = {}

    def get_csv(self, file_path):
        current_time = time.time()
        if file_path in self.cache:
            if current_time - self.last_update.get(file_path, 0) < self.ttl:
                return self.cache[file_path]
        if not os.path.exists(file_path):
            return pd.DataFrame()
        try:
            df = pd.read_csv(file_path)
            self.cache[file_path] = df
            self.last_update[file_path] = current_time
            return df
        except Exception as e:
            print(f"Cache load error for {file_path}: {e}")
            return pd.DataFrame()
            
    def get(self, key):
        current_time = time.time()
        if key in self.cache:
            if current_time - self.last_update.get(key, 0) < self.ttl:
                return self.cache[key]
        return None

    def set(self, key, value):
        self.cache[key] = value
        self.last_update[key] = time.time()

data_cache = DataCache(ttl_seconds=60)

app = Flask(__name__)

# 1. Force HTML templates to reload instantly (disable to make faster)
app.config['TEMPLATES_AUTO_RELOAD'] = True

# 2. Force CSS/JS to never cache (Bulletproof method, disable to make faster)
@app.after_request
def add_header(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


CORS(app) # This allows your JS chart to talk to your API without security errors

# --- CONFIGURATION ---
from config import NTFY_URL          # phone alerts; empty = off

# ==========================================
# 1. MATH & LOGIC FILTERS
# ==========================================
def is_out_of_the_money(opt_type, strike, underlying_price):
    """Deep ITM options are usually hedges. We want directional OTM bets."""
    if opt_type == 'call':
        return strike > underlying_price
    elif opt_type == 'put':
        return strike < underlying_price
    return False

def calculate_vol_oi_ratio(volume, open_interest):
    """Finds 'New Blood'. If Volume > OI, new positions are opening live."""
    if pd.isna(volume) or volume <= 0:
        return 0.0
    if pd.isna(open_interest) or open_interest == 0:
        return 999.0 # Brand new strike being swept
    return round(volume / open_interest, 2)

def calculate_premium_spent(volume, last_price):
    """Calculates the estimated dollar value of the sweep."""
    if pd.isna(volume) or pd.isna(last_price):
        return 0.0
    return volume * last_price * 100 # Options represent 100 shares

# ==========================================
# 2. DATA HARVESTING ENGINE
# ==========================================
def get_current_price(ticker_symbol):
    """Fetches the live underlying stock price."""
    ticker = yf.Ticker(ticker_symbol)
    todays_data = ticker.history(period='1d')
    if todays_data.empty:
        raise ValueError(f"Could not fetch live price for {ticker_symbol}.")
    return todays_data['Close'].iloc[0]

def get_chains(ticker_symbol, max_days_out=None):
    """Fetches options chains. If max_days_out is None, fetches EVERYTHING."""
    ticker = yf.Ticker(ticker_symbol)
    expirations = ticker.options
    if not expirations:
        return pd.DataFrame()

    today = datetime.now()
    valid_chains = []

    for exp in expirations:
        exp_date = datetime.strptime(exp, "%Y-%m-%d")
        days_to_exp = (exp_date - today).days
        
        # Filter by days out if a limit is set
        if max_days_out is None or (0 <= days_to_exp <= max_days_out):
            try:
                chain = ticker.option_chain(exp)
                chain.calls['expiration'] = exp
                chain.calls['type'] = 'call'
                chain.puts['expiration'] = exp
                chain.puts['type'] = 'put'
                valid_chains.append(chain.calls)
                valid_chains.append(chain.puts)
            except Exception:
                continue

    if not valid_chains:
        return pd.DataFrame()
    return pd.concat(valid_chains, ignore_index=True)

# ==========================================
# 3. NOTIFICATION DISPATCHER
# ==========================================
def send_whale_alert(ticker, contract, time_context):
    """Pushes a high-priority alert to the iPhone."""
    emoji = "🔥 CALL" if contract['type'] == 'call' else "🩸 PUT"
    title = f"{ticker} {time_context} WHALE |"
    body = (
        f"${emoji}: \n",
        f"Strike: ${contract['strike']} | Exp: {contract['expiration']}\n"
        f"Volume: {contract['volume']:,} vs OI: {contract['open_interest']:,}\n"
        f"Vol/OI Ratio: {contract['vol_oi_ratio']}x\n"
        f"Est. Premium: ${contract['premium_spent']:,.2f}"
    )
    headers = {
        "Title": title,
        "Priority": "high",
        "Tags": "whale,rotating_light"
    }
    if not NTFY_URL:
        return
    try:
        requests.post(NTFY_URL, data=body.encode('utf-8'), headers=headers)
        print(f"Alert sent for {contract['symbol']}")
    except Exception as e:
        print(f"Failed to send alert: {e}")

# ==========================================
# 4. STRATEGY EXECUTORS
# ==========================================
def execute_morning_hunt(ticker):
    """8:31 AM LOGIC: Short DTE, High Urgency, >$100k Premium"""
    print(f"Executing Morning Hunt for {ticker}...")
    current_price = get_current_price(ticker)
    df = get_chains(ticker, max_days_out=14)
    
    if df.empty: return []
    whales_caught = []

    for index, row in df.iterrows():
        # Grab all the standard and new data points
        opt_type = row['type']
        strike = row['strike']
        vol = row['volume']
        oi = row['openInterest']
        last_price = row['lastPrice']
        bid = row['bid']
        ask = row['ask']
        iv = row.get('impliedVolatility', 0.0) # Safe extraction

        if not is_out_of_the_money(opt_type, strike, current_price): continue
        
        ratio = calculate_vol_oi_ratio(vol, oi)
        if ratio < 1.5: continue
        
        premium = calculate_premium_spent(vol, last_price)
        if premium < 100000: continue

        # Build the expanded dictionary
        whale_data = {
            "symbol": row['contractSymbol'], 
            "type": opt_type, 
            "expiration": row['expiration'],
            "strike": strike, 
            "volume": int(vol), 
            "open_interest": int(oi),
            "vol_oi_ratio": ratio, 
            "premium_spent": premium,
            "last_price": last_price,
            "bid": bid,
            "ask": ask,
            "iv": iv
        }
        whales_caught.append(whale_data)
        send_whale_alert(ticker, whale_data, "🌅 MORNING")

    return sorted(whales_caught, key=lambda x: x['premium_spent'], reverse=True)


def execute_evening_hunt(ticker):
    """2:00 PM LOGIC: All DTEs, Massive Blocks/Positioning, >$500k Premium"""
    print(f"Executing Evening Hunt for {ticker}...")
    current_price = get_current_price(ticker)
    df = get_chains(ticker, max_days_out=None) 
    
    if df.empty: return []
    whales_caught = []

    for index, row in df.iterrows():
        # Grab all the standard and new data points
        opt_type = row['type']
        strike = row['strike']
        vol = row['volume']
        oi = row['openInterest']
        last_price = row['lastPrice']
        bid = row['bid']
        ask = row['ask']
        iv = row.get('impliedVolatility', 0.0) # Safe extraction

        if not is_out_of_the_money(opt_type, strike, current_price): continue
        
        ratio = calculate_vol_oi_ratio(vol, oi)
        if ratio < 1.0: continue 
        
        premium = calculate_premium_spent(vol, last_price)
        if premium < 500000: continue 

        # Build the expanded dictionary
        whale_data = {
            "symbol": row['contractSymbol'], 
            "type": opt_type, 
            "expiration": row['expiration'],
            "strike": strike, 
            "volume": int(vol), 
            "open_interest": int(oi),
            "vol_oi_ratio": ratio, 
            "premium_spent": premium,
            "last_price": last_price,
            "bid": bid,
            "ask": ask,
            "iv": iv
        }
        whales_caught.append(whale_data)
        send_whale_alert(ticker, whale_data, "🌆 EVENING")

    return sorted(whales_caught, key=lambda x: x['premium_spent'], reverse=True)

def execute_custom_hunt(ticker, min_vol_oi, min_premium, max_dte):
    """CUSTOM LOGIC: User-defined thresholds passed via URL"""
    print(f"Executing Custom Hunt for {ticker} | Vol/OI: {min_vol_oi} | Premium: {min_premium} | DTE: {max_dte}")
    current_price = get_current_price(ticker)
    df = get_chains(ticker, max_days_out=max_dte)
    
    if df.empty: return []
    whales_caught = []

    for index, row in df.iterrows():
        # Grab all the standard and new data points
        opt_type = row['type']
        strike = row['strike']
        vol = row['volume']
        oi = row['openInterest']
        last_price = row['lastPrice']
        bid = row['bid']
        ask = row['ask']
        iv = row.get('impliedVolatility', 0.0) # Safe extraction

        if not is_out_of_the_money(opt_type, strike, current_price): continue
        
        ratio = calculate_vol_oi_ratio(vol, oi)
        if ratio < min_vol_oi: continue 
        
        premium = calculate_premium_spent(vol, last_price)
        if premium < min_premium: continue 

        # Build the expanded dictionary
        whale_data = {
            "symbol": row['contractSymbol'], 
            "type": opt_type, 
            "expiration": row['expiration'],
            "strike": strike, 
            "volume": int(vol), 
            "open_interest": int(oi),
            "vol_oi_ratio": ratio, 
            "premium_spent": premium,
            "last_price": last_price,
            "bid": bid,
            "ask": ask,
            "iv": iv
        }
        whales_caught.append(whale_data)
        send_whale_alert(ticker, whale_data, "🛠 CUSTOM")

    return sorted(whales_caught, key=lambda x: x['premium_spent'], reverse=True)

# ==========================================
# 5. FLASK API ROUTER
# ==========================================
def build_xml_response(ticker, strategy, whales):
    """Helper to convert the Python list of dicts into strict XML."""
    root = ET.Element("whale_hunt", ticker=ticker, strategy=strategy, whale_count=str(len(whales)))
    for w in whales:
        contract = ET.SubElement(root, "contract", symbol=w['symbol'], type=w['type'].upper(), expiration=w['expiration'])
        contract.set("strike", str(w['strike']))
        
        # New Pricing & Spread Data
        contract.set("last_price", f"${w['last_price']:.2f}")
        contract.set("bid", f"${w['bid']:.2f}")
        contract.set("ask", f"${w['ask']:.2f}")
        
        # Calculate the spread safely (avoiding negative or weird zero errors)
        spread = max(0.0, w['ask'] - w['bid'])
        contract.set("spread", f"${spread:.2f}")
        
        # Volume & Institutional Metrics
        contract.set("volume", str(w['volume']))
        contract.set("open_interest", str(w['open_interest']))
        contract.set("vol_oi_ratio", f"{w['vol_oi_ratio']}x")
        
        # IV and Total Capital
        contract.set("implied_volatility", f"{w['iv'] * 100:.2f}%")
        contract.set("premium_spent", f"${w['premium_spent']:,.2f}")

    xml_str = minidom.parseString(ET.tostring(root, encoding='utf-8')).toprettyxml(indent="  ")
    return Response(xml_str, mimetype='application/xml')

#--- BLACK-SCHOLES GAMMA CALCULATOR ---
def calculate_gamma(S, K, T, r, sigma):
    """
    S = Spot Price
    K = Strike Price
    T = Time to Expiration (in years)
    r = Risk-free rate
    sigma = Implied Volatility
    """
    # Prevent division by zero for expired options or zero vol
    if T <= 0 or sigma <= 0:
        return 0.0

    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    
    # PDF of standard normal distribution
    nd1 = norm.pdf(d1) 
    
    gamma = nd1 / (S * sigma * np.sqrt(T))
    return gamma

@app.route('/api/gex')
def get_gex_profile():
    ticker_symbol = request.args.get('ticker', 'SPY').upper()
    
    try:
        ticker = yf.Ticker(ticker_symbol)
        spot_price = ticker.info.get('regularMarketPrice') or ticker.history(period='1d')['Close'].iloc[-1]
        
        # Get all available expiration dates
        expirations = ticker.options
        if not expirations:
            return jsonify({"status": "error", "message": "No options data available."})
            
        # For a standard GEX profile, quants usually look at the front month or 0DTE.
        # We will grab the 3 closest expirations to build a thick profile.
        target_exps = expirations[:3] 
        
        gex_by_strike = {}
        risk_free_rate = 0.05 # Assuming ~5% risk-free rate
        
        for exp in target_exps:
            opt_chain = ticker.option_chain(exp)
            calls = opt_chain.calls
            puts = opt_chain.puts
            
            # Calculate Time to Expiration (T) in years
            # Approximation: (Expiration Date - Today) / 365
            import datetime
            exp_date = datetime.datetime.strptime(exp, '%Y-%m-%d')
            today = datetime.datetime.today()
            days_to_exp = (exp_date - today).days
            T = max(days_to_exp / 365.0, 0.001) # Minimum 1 day to prevent math errors
            
            # Process Calls (Positive Gamma)
            for _, row in calls.iterrows():
                strike = row['strike']
                oi = row['openInterest']
                iv = row['impliedVolatility']
                
                if oi > 0 and iv > 0.01:
                    gamma = calculate_gamma(spot_price, strike, T, risk_free_rate, iv)
                    # Dealer GEX Assumption: Dealers sell calls to retail, so they are short calls (negative gamma).
                    # Standard convention flips this for the chart: Calls = Positive GEX, Puts = Negative GEX
                    contract_gex = gamma * oi * 100 * spot_price
                    
                    gex_by_strike[strike] = gex_by_strike.get(strike, 0) + contract_gex

            # Process Puts (Negative Gamma)
            for _, row in puts.iterrows():
                strike = row['strike']
                oi = row['openInterest']
                iv = row['impliedVolatility']
                
                if oi > 0 and iv > 0.01:
                    gamma = calculate_gamma(spot_price, strike, T, risk_free_rate, iv)
                    contract_gex = gamma * oi * 100 * spot_price
                    
                    gex_by_strike[strike] = gex_by_strike.get(strike, 0) - contract_gex

        # Filter the strikes to only show a realistic window (e.g., +/- 10% from spot)
        lower_bound = spot_price * 0.90
        upper_bound = spot_price * 1.10
        
        filtered_strikes = {k: v for k, v in gex_by_strike.items() if lower_bound <= k <= upper_bound}
        
        # Sort strikes from lowest to highest
        sorted_strikes = sorted(filtered_strikes.keys())
        gamma_values = [filtered_strikes[k] for k in sorted_strikes]
        
        # Find the Walls
        call_wall_strike = max(filtered_strikes, key=filtered_strikes.get) if filtered_strikes else 0
        put_wall_strike = min(filtered_strikes, key=filtered_strikes.get) if filtered_strikes else 0
        
        # Approximate Zero Gamma (Flip point)
        # Find where the cumulative sum of gamma changes sign, or just the spot where it crosses 0
        zero_gamma = spot_price # Rough approximation for UI, real calc is complex root finding

        payload = {
            "spot": spot_price,
            "zeroGamma": zero_gamma,
            "callWall": call_wall_strike,
            "putWall": put_wall_strike,
            "strikes": sorted_strikes,
            "gamma": gamma_values
        }

        return jsonify({"status": "success", "data": payload})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/arbitrage_history', methods=['GET'])
def get_arbitrage_history():
    try:
        if not os.path.exists(LEDGER_FILE):
            return jsonify({"status": "error", "message": "Ledger file not found."}), 404

        # 1. Ingestion: Load the CSV
        df = pd.read_csv(LEDGER_FILE)
        
        if df.empty:
            return jsonify({"status": "error", "message": "Ledger is empty."}), 404

        # 2. Sanitization: Limit the data points to prevent terminal lag
        # Defaults to the last 50 data points, but UI can request more via ?limit=100
        limit = int(request.args.get('limit', 50))
        df = df.tail(limit).copy()

        # Format Datetime for cleaner Chart.js X-Axis (e.g., '03-24 14:30')
        df['Datetime'] = pd.to_datetime(df['Datetime']).dt.strftime('%m-%d %H:%M')

        # Safely handle NaNs (replaces pandas NaN with Python None, which becomes JSON 'null')
        # This ensures Chart.js simply leaves a gap instead of crashing if a value is missing
        df = df.astype(object).where(pd.notnull(df), None)

        # 3. Payload Architecture: Parallel arrays for Chart.js
        payload = {
            "labels": df['Datetime'].tolist(),
            "spot": df['COMEX_Spot'].tolist(),
            "cheapest_price": df['Cheapest_Eagle'].tolist(),
            "avg_price": df['Average_Eagle'].tolist(),
            "cheapest_pct": df['Cheapest_Premium_Percent'].tolist(),
            "avg_pct": df['Average_Premium_Percent'].tolist(),
            "cheapest_dollar": df['Cheapest_Premium_Dollars'].tolist()
        }

        return jsonify({
            "status": "success", 
            "data": payload
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# Initialize the Databento Client
DB_API_KEY = required_env("DATABENTO_API_KEY", alt_name="DB_API_KEY")
db_client = db.Historical(DB_API_KEY)

@app.route('/api/darkpool')
def get_dark_pool_profile():
    ticker = request.args.get('ticker', 'SPY').upper()
    
    # --- THE T+1 HISTORICAL BARRIER FIX ---
    now = datetime.utcnow()
    
    # Databento's Historical API batches the tape overnight. 
    # We must anchor our 'end' to Midnight UTC of YESTERDAY to guarantee the file exists.
    yesterday = now - timedelta(days=1)
    available_end = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    
    # Find the most recently completed trading session
    # weekday(): 0=Mon, 1=Tue, ..., 5=Sat, 6=Sun
    if available_end.weekday() == 6: # Sunday Midnight UTC -> Shift to Saturday Midnight
        end_time = available_end - timedelta(days=1)
    elif available_end.weekday() == 0: # Monday Midnight UTC -> Shift to Saturday Midnight
        end_time = available_end - timedelta(days=2)
    else:
        end_time = available_end
        
    # Look back exactly 24 hours from our safe 'end_time' to capture the full session
    start_time = end_time - timedelta(days=1)
    
    try:
        # 1. THE QUERY: Fetch tick-level trades from the Consolidated Tape
        data = db_client.timeseries.get_range(
            dataset='DBEQ.BASIC',
            schema='trades',       
            symbols=[ticker],
            start=start_time.isoformat(),
            end=end_time.isoformat(),
            limit=50000            
        )
        
        # Convert the raw binary stream into a Pandas DataFrame
        df = data.to_df()
        
        if df.empty:
            return jsonify({"status": "error", "message": "No trades found in the target window."})

        # 2. THE FILTER: Isolate the Whales
        blocks = df[df['size'] >= 10000].copy()
        
        if blocks.empty:
            return jsonify({"status": "success", "message": "No institutional blocks detected.", "data": None})

        # 3. THE MATH: Calculate the Profile Metrics
        total_block_volume = int(blocks['size'].sum())
        total_notional = float((blocks['price'] * blocks['size']).sum())
        largest_block = int(blocks['size'].max())
        avg_block_price = float((blocks['price'] * blocks['size']).sum() / total_block_volume)

        # 4. THE SENTIMENT ENGINE (Bullish vs Bearish)
        bullish_vol = int(blocks[blocks['side'] == 'A']['size'].sum())
        bearish_vol = int(blocks[blocks['side'] == 'B']['size'].sum())
        
        sentiment = "NEUTRAL"
        if bullish_vol > bearish_vol * 1.2: sentiment = "BULLISH"
        elif bearish_vol > bullish_vol * 1.2: sentiment = "BEARISH"

        # 5. THE PAYLOAD
        payload = {
            "ticker": ticker,
            "total_block_volume": total_block_volume,
            "total_notional_usd": total_notional,
            "largest_single_block": largest_block,
            "vwap_price": avg_block_price,
            "sentiment": {
                "bias": sentiment,
                "bull_volume": bullish_vol,
                "bear_volume": bearish_vol
            },
            "recent_prints": []
        }

        # Grab the 5 most recent massive prints
        recent_trades = blocks.tail(5).sort_index(ascending=False)
        for index, row in recent_trades.iterrows():
            payload["recent_prints"].append({
                "time": index.strftime("%H:%M:%S"),
                "price": float(row['price']),
                "size": int(row['size']),
                "side": "BUY" if row['side'] == 'A' else "SELL" if row['side'] == 'B' else "UNK"
            })

        return jsonify({"status": "success", "data": payload})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

# --- ENDPOINT: VMRI DATA PROXY ---
@app.route('/api/vmri_history')
def get_vmri_history():
    """Returns the time-series history of VMRI scores, math derivatives, and macro context."""
    try:
        if not os.path.exists(LEDGER_CSV):
            return jsonify({"error": "Ledger not found"}), 404
            
        df = pd.read_csv(LEDGER_CSV)
        
        # 1. THE FIX: Drop completely empty rows or rows missing a Datetime BEFORE parsing
        df = df.dropna(how='all')
        df = df.dropna(subset=['Datetime'])
        
        df['Datetime'] = pd.to_datetime(df['Datetime'], format='mixed')
        
        # Drop any dates that failed to parse (NaT)
        df = df.dropna(subset=['Datetime'])
        
        df = df.sort_values('Datetime').tail(500) # Last 500 records

        # ---------------------------------------------------------
        # THE MATH ENGINE (Phase 1 Upgrades)
        # ---------------------------------------------------------
        
        # 1. Moving Average (10-Period SMA)
        # Acts as a mechanical trendline/crossover trigger
        df['VMRI_SMA_10'] = df['VMRI_Score'].rolling(window=10).mean()

        # 2. Momentum / Rate of Change (5-Period Delta)
        # Measures velocity. Positive = accelerating risk, Negative = decaying risk
        df['VMRI_Momentum_5'] = df['VMRI_Score'].diff(periods=5)

        # 3. The "Primary Driver" Logic
        # Look at the absolute % change of the 4 core pillars over the last 5 periods.
        drivers_df = pd.DataFrame({
            'DXY': pd.to_numeric(df['DXY'], errors='coerce').pct_change(periods=5).abs(),
            '10Y Yield': pd.to_numeric(df['10Y_Yield'], errors='coerce').pct_change(periods=5).abs(),
            'VIX': pd.to_numeric(df['VIX'], errors='coerce').pct_change(periods=5).abs(),
            'High Yield OAS': pd.to_numeric(df['High_Yield_OAS'], errors='coerce').pct_change(periods=5).abs()
        })
        
        # Safely find the max, ignoring rows that are entirely NaN (like the first 5 rows)
        def get_driver(row):
            if row.isna().all():
                return "AWAITING DATA"
            return row.idxmax()
            
        df['Primary_Driver'] = drivers_df.apply(get_driver, axis=1)
        
        # ---------------------------------------------------------

        labels = df['Datetime'].dt.strftime('%Y-%m-%d %H:%M').tolist()
        
        # THE ULTIMATE LIST CLEANER
        def clean_list(lst):
            cleaned = []
            for val in lst:
                if pd.isna(val) or val in ["NaN", "nan", "None", ""]:
                    cleaned.append(None)
                else:
                    try:
                        f = float(val)
                        if math.isnan(f) or math.isinf(f):
                            cleaned.append(None)
                        else:
                            cleaned.append(f)
                    except (ValueError, TypeError):
                        cleaned.append(None)
            return cleaned
        
        # Ensure string columns (like drivers) safely handle NaNs before tolist()
        driver_list = df['Primary_Driver'].astype(str).tolist()

        data = {
            "labels": labels,
            "scores": clean_list(df['VMRI_Score'].tolist()),
            
            # New Math Data
            "sma_10": clean_list(df['VMRI_SMA_10'].tolist()),
            "momentum_5": clean_list(df['VMRI_Momentum_5'].tolist()),
            "primary_driver": driver_list,
            
            "context": {
                "dxy": clean_list(df['DXY'].tolist()),
                "yield": clean_list(df['10Y_Yield'].tolist()),
                "vix": clean_list(df['VIX'].tolist()),
                "gold": clean_list(df['Gold_Price'].tolist()),
                "gsr": clean_list(df['Gold_Silver_Ratio'].tolist()),
                "oas": clean_list(df['High_Yield_OAS'].tolist())
            }
        }
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- ENDPOINT: VMRI CHART VIEWER ---
@app.route('/vmri_chart')
def vmri_chart_page():
    """Serves a professional-grade interactive risk monitor."""
    html_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>VMRI Systemic Risk Monitor</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@2.1.0/dist/chartjs-plugin-annotation.min.js"></script>
        <style>
            body { font-family: 'Courier New', monospace; background: transparent; color: #00ff00; padding: 10px; margin: 0; box-sizing: border-box; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
            .container { flex: 1; display: flex; flex-direction: column; background: #000; padding: 10px; border: 1px solid #1a1a1a; border-radius: 4px; position: relative; }
            .header-bar { display: flex; justify-content: space-between; items-center: center; margin-bottom: 5px; }
            h2 { color: #ff0000; letter-spacing: 3px; font-size: 14px; margin: 0; }
            
            /* Sleek Toggles */
            .toggles { display: flex; gap: 5px; }
            .overlay-btn { background: #111; border: 1px solid #333; color: #666; font-family: monospace; font-size: 9px; padding: 2px 6px; cursor: pointer; border-radius: 3px; transition: all 0.2s; }
            .overlay-btn:hover { border-color: #888; color: #ccc; }
            .overlay-btn.active { background: rgba(255, 255, 255, 0.1); color: #fff; border-color: #fff; box-shadow: 0 0 5px rgba(255,255,255,0.3); }
            
            .chart-wrapper { flex: 1; position: relative; min-height: 0; }
            
            /* The Insight Banner */
            .insight-banner { margin-top: 5px; padding: 6px; background: #0a0a0a; border: 1px solid #222; border-radius: 3px; font-size: 9px; color: #aaa; text-align: center; font-weight: bold; letter-spacing: 1px; }
            .insight-threat { color: #ff4444; }
            .insight-safe { color: #44ff44; }
            .insight-driver { color: #38bdf8; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header-bar">
                <h2>🚨 VMRI SYSTEMIC RISK MONITOR</h2>
                <div class="toggles">
                    <button id="btnDXY" class="overlay-btn" onclick="toggleOverlay('dxy', this)">+ DXY</button>
                    <button id="btnVIX" class="overlay-btn" onclick="toggleOverlay('vix', this)">+ VIX</button>
                    <button id="btnYield" class="overlay-btn" onclick="toggleOverlay('yield', this)">+ 10Y</button>
                    <button id="btnOAS" class="overlay-btn" onclick="toggleOverlay('oas', this)">+ OAS</button>
                </div>
            </div>
            
            <div class="chart-wrapper">
                <canvas id="vmriChart"></canvas>
            </div>
            
            <div id="insightBanner" class="insight-banner">
                AWAITING TELEMETRY...
            </div>
        </div>

        <script>
            let vmriChartInstance = null;
            let masterData = null;
            let activeOverlay = null;

            async function loadChart() {
                try {
                    const response = await fetch('/api/vmri_history');
                    masterData = await response.json();
                    
                    // Safety Check: Did the API return an error?
                    if (masterData.error) {
                        console.error("API Error:", masterData.error);
                        document.getElementById('insightBanner').innerHTML = `<span class="insight-threat">⚠️ API ERROR: ${masterData.error}</span>`;
                        return;
                    }
                    
                    renderChart();
                    updateInsightBanner();
                } catch (e) { 
                    console.error("Fetch failed:", e); 
                    document.getElementById('insightBanner').innerHTML = `<span class="insight-threat">⚠️ CONNECTION FAILED</span>`;
                }
            }

            function updateInsightBanner() {
                const lastIdx = masterData.scores.length - 1;
                const currentScore = masterData.scores[lastIdx];
                const momentum = masterData.momentum_5[lastIdx];
                const driver = masterData.primary_driver[lastIdx];

                let statusHtml = '';
                if (currentScore >= 250) {
                    let trend = momentum > 0 ? "ACCELERATING UPWARD" : "DECAYING";
                    statusHtml = `<span class="insight-threat">⚠️ SYSTEMIC THREAT ACTIVE (${currentScore.toFixed(0)})</span> | Primary Driver: <span class="insight-driver">${driver}</span> | Trend: ${trend}`;
                } else {
                    let trend = momentum < 0 ? "COOLING" : "BUILDING";
                    statusHtml = `<span class="insight-safe">🟢 RISK ON ENVIRONMENT (${currentScore.toFixed(0)})</span> | Primary Driver: <span class="insight-driver">${driver}</span> | Trend: ${trend}`;
                }
                
                document.getElementById('insightBanner').innerHTML = statusHtml;
            }

            function toggleOverlay(metric, btnElement) {
                // Clear active states
                document.querySelectorAll('.overlay-btn').forEach(btn => btn.classList.remove('active'));
                
                if (activeOverlay === metric) {
                    // Turn it off if already active
                    activeOverlay = null;
                } else {
                    // Turn it on
                    activeOverlay = metric;
                    btnElement.classList.add('active');
                }
                renderChart();
            }

            function renderChart() {
                const ctx = document.getElementById('vmriChart').getContext('2d');
                if (vmriChartInstance) vmriChartInstance.destroy();

                let gradient = ctx.createLinearGradient(0, 0, 0, 400);
                gradient.addColorStop(0, 'rgba(255, 0, 0, 0.4)');
                gradient.addColorStop(0.6, 'rgba(255, 165, 0, 0.1)');
                gradient.addColorStop(1, 'rgba(0, 255, 0, 0.02)');

                // 1. Base VMRI Dataset
                const datasets = [
                    {
                        label: 'VMRI Score',
                        data: masterData.scores,
                        borderColor: '#00ff00',
                        borderWidth: 2,
                        fill: false, // Turn off fill so we can see the banding clearly
                        tension: 0.2,
                        pointRadius: 0,
                        pointHoverRadius: 4,
                        yAxisID: 'y'
                    },
                    {
                        label: '10-Period SMA',
                        data: masterData.sma_10,
                        borderColor: 'rgba(255, 255, 255, 0.4)',
                        borderWidth: 1,
                        borderDash: [5, 5],
                        pointRadius: 0,
                        yAxisID: 'y'
                    }
                ];

                // 2. Dynamic Overlay Dataset
                if (activeOverlay) {
                    let overlayData = masterData.context[activeOverlay];
                    let overlayLabel = activeOverlay.toUpperCase();
                    
                    datasets.push({
                        label: overlayLabel + ' (Overlay)',
                        data: overlayData,
                        borderColor: '#38bdf8', // Neon Blue
                        borderWidth: 1.5,
                        borderDash: [2, 2],
                        pointRadius: 0,
                        yAxisID: 'y1' // Use secondary axis
                    });
                }

                vmriChartInstance = new Chart(ctx, {
                    type: 'line',
                    data: {
                        labels: masterData.labels,
                        datasets: datasets
                    },
                    options: {
                        devicePixelRatio: 3,
                        responsive: true,
                        maintainAspectRatio: false,
                        interaction: { mode: 'index', intersect: false },
                        plugins: {
                            legend: { display: false },
                            // --- RISK REGIME BANDING ---
                            annotation: {
                                annotations: {
                                    box1: { type: 'box', yMin: 0, yMax: 150, backgroundColor: 'rgba(34, 197, 94, 0.05)', borderWidth: 0 },
                                    box2: { type: 'box', yMin: 150, yMax: 250, backgroundColor: 'rgba(234, 179, 8, 0.05)', borderWidth: 0 },
                                    box3: { type: 'box', yMin: 250, yMax: 350, backgroundColor: 'rgba(249, 115, 22, 0.05)', borderWidth: 0 },
                                    box4: { type: 'box', yMin: 350, yMax: 1000, backgroundColor: 'rgba(239, 68, 68, 0.05)', borderWidth: 0 }
                                }
                            },
                            tooltip: {
                                backgroundColor: 'rgba(10, 10, 10, 0.95)',
                                titleFont: { size: 10, family: 'monospace' },
                                titleColor: '#aaa',
                                bodyFont: { family: 'monospace', size: 11 },
                                borderColor: '#333',
                                borderWidth: 1,
                                padding: 10,
                                callbacks: {
                                    label: function(context) {
                                        let label = context.dataset.label || '';
                                        if (label) { label += ': '; }
                                        if (context.parsed.y !== null) { label += context.parsed.y.toFixed(2); }
                                        return label;
                                    }
                                }
                            }
                        },
                        scales: {
                            x: { display: false },
                            y: { 
                                min: 50, 
                                max: 400, // Lock axis so banding stays consistent
                                grid: { color: '#111' }, 
                                ticks: { color: '#666', font: {size: 9} } 
                            },
                            y1: {
                                display: activeOverlay ? true : false,
                                position: 'right',
                                grid: { display: false },
                                ticks: { color: '#38bdf8', font: {size: 9} }
                            }
                        }
                    }
                });
            }
            
            loadChart();
        </script>
    </body>
    </html>
    """
    return render_template_string(html_template)

@app.route('/api/war_room', methods=['GET', 'POST'])
def api_war_room():
    """
    The SecDB Lite Impact Engine.
    Accepts shift vectors for DXY, 10Y Yield, OAS, and VIX.
    Recalculates the VMRI and returns the hypothetical environment.
    """
    try:
        # Handle both GET (URL params) and POST (JSON body)
        if request.method == 'POST':
            data = request.get_json() or {}
        else:
            data = request.args

        # 1. Parse Shift Vectors
        dxy_shift = float(data.get('dxy_shift', 0.0))
        tnx_shift = float(data.get('tnx_shift', 0.0))
        oas_shift = float(data.get('oas_shift', 0.0))
        vix_shift = float(data.get('vix_shift', 0.0))
        vix_shift_pct = float(data.get('vix_shift_pct', 0.0)) 

        # 2. Ingest the Latest Valid Live Environment
        df = pd.read_csv(LEDGER_CSV)
        # CRITICAL FIX: Drop completely empty rows that might be at the end of the CSV
        df = df.dropna(how='all') 
        latest = df.iloc[-1]

        # Helper to safely extract floats, with a secondary check to crawl up the CSV if needed
        def safe_float(col_name, fallback):
            val = latest.get(col_name)
            try:
                f = float(val)
                if pd.notna(f):
                    return f
            except:
                pass
                
            # If the absolute last row had a NaN for this specific column, 
            # crawl backwards up the CSV to find the last known good value.
            try:
                last_valid = df[col_name].dropna().iloc[-1]
                return float(last_valid)
            except:
                return fallback

        # Pull the real data (or crawl back to find it)
        base_dxy = safe_float('DXY', 100.0)
        base_tnx = safe_float('10Y_Yield', 4.0)
        base_oas = safe_float('High_Yield_OAS', 4.0)
        base_vix = safe_float('VIX', 20.0)
        base_vmri = safe_float('VMRI_Score', 200.0)

        # 3. Apply the Shift Vectors to create the Hypothetical Environment
        hypo_dxy = base_dxy + dxy_shift
        hypo_tnx = base_tnx + tnx_shift
        hypo_oas = base_oas + oas_shift
        
        # Calculate VIX shift 
        if vix_shift_pct != 0.0:
            hypo_vix = base_vix * (1 + (vix_shift_pct / 100.0))
        else:
            hypo_vix = base_vix + vix_shift

        # 4. The Math Engine: Recalculate VMRI
        def parts(dxy, tnx, oas, vix):
            base, credit, vol = (dxy * tnx) / 1.61, oas / 4.00, vix / 20.00
            return base, credit, vol, base * credit * vol

        def tier_of(v):
            if v < 150:
                return "LOW RISK (Complacent / Squeeze Danger)"
            if v < 250:
                return "MODERATE RISK (Standard Operating Environment)"
            if v < 350:
                return "ELEVATED RISK (Hedge Triggers Active)"
            return "SYSTEMIC THREAT (Crash Dynamics Active)"

        base_stress, credit_multiplier, vol_premium, hypo_vmri = parts(hypo_dxy, hypo_tnx, hypo_oas, hypo_vix)
        live_stress, live_credit, live_vol, live_formula_vmri = parts(base_dxy, base_tnx, base_oas, base_vix)

        # 5. Determine the New Threat Tier
        tier = tier_of(hypo_vmri)

        # 6. Calculate the Impact Delta
        vmri_delta = hypo_vmri - base_vmri
        vmri_delta_pct = (vmri_delta / base_vmri) * 100 if base_vmri != 0 else 0

        # What each lever does on its own (the others left at live values). The four effects do not add up to the
        # total because the variables are multiplied.
        solo = {
            "dxy": parts(hypo_dxy, base_tnx, base_oas, base_vix)[3] - live_formula_vmri,
            "tnx": parts(base_dxy, hypo_tnx, base_oas, base_vix)[3] - live_formula_vmri,
            "oas": parts(base_dxy, base_tnx, hypo_oas, base_vix)[3] - live_formula_vmri,
            "vix": parts(base_dxy, base_tnx, base_oas, hypo_vix)[3] - live_formula_vmri,
        }

        # Where the live and scenario scores sit among the scores this installation has recorded
        history = None
        scores = pd.to_numeric(df.get('VMRI_Score'), errors='coerce').dropna() if 'VMRI_Score' in df else pd.Series(dtype=float)
        if len(scores) >= 20:
            when = pd.to_datetime(df.loc[scores.index, 'Datetime'], errors='coerce', format='mixed').dropna()
            lo, hi = math.floor(scores.min() / 10) * 10, math.ceil(max(scores.max(), 360) / 10) * 10
            counts, edges = np.histogram(scores, bins=30, range=(lo, hi))
            history = {
                "n": int(len(scores)), "start": when.min().date().isoformat() if len(when) else None,
                "end": when.max().date().isoformat() if len(when) else None,
                "min": round(float(scores.min()), 1), "median": round(float(scores.median()), 1), "max": round(float(scores.max()), 1),
                "pct_below_current": round(float((scores < base_vmri).mean() * 100), 1),
                "pct_below_hypothetical": round(float((scores < hypo_vmri).mean() * 100), 1),
                "lo": lo, "hi": hi, "counts": [int(c) for c in counts],
            }

        # 7. Construct the Output Payload
        payload = {
            "status": "success",
            "formula": "VMRI = (DXY x 10Y yield / 1.61) x (HY OAS / 4) x (VIX / 20)",
            "thresholds": {"moderate": 150, "elevated": 250, "systemic": 350},
            "current": {
                "vmri": round(base_vmri, 2),
                "tier": tier_of(base_vmri),
                "dxy": round(base_dxy, 2),
                "tnx": round(base_tnx, 2),
                "oas": round(base_oas, 2),
                "vix": round(base_vix, 2),
                "factors": {"macro_base": round(live_stress, 2), "credit": round(live_credit, 3), "vol": round(live_vol, 3)}
            },
            "hypothetical": {
                "vmri": round(hypo_vmri, 2),
                "tier": tier,
                "dxy": round(hypo_dxy, 2),
                "tnx": round(hypo_tnx, 2),
                "oas": round(hypo_oas, 2),
                "vix": round(hypo_vix, 2),
                "factors": {"macro_base": round(base_stress, 2), "credit": round(credit_multiplier, 3), "vol": round(vol_premium, 3)}
            },
            "impact": {
                "vmri_delta": round(vmri_delta, 2),
                "vmri_delta_pct": round(vmri_delta_pct, 2),
                "solo_delta": {k: round(v, 2) for k, v in solo.items()}
            },
            "history": history
        }

        return jsonify(payload)

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/silver_eagle_prices', methods=['GET'])
def api_silver_eagle_prices():
    try:
        # Run the external ebay.py script
        output = subprocess.check_output(
            [PYTHON_BIN, EBAY_SCRIPT_PATH], 
            text=True, 
            timeout=120 # Playwright takes time, so give it a 2-minute timeout
        )
        
        # The script prints pure XML to stdout, so we just return it
        return Response(output, mimetype='application/xml')

    except subprocess.TimeoutExpired:
        return Response("<error>Scrape timed out. eBay might be blocking connections.</error>", mimetype='application/xml', status=504)
    except subprocess.CalledProcessError as e:
        return Response(f"<error>Script crashed: {e.output}</error>", mimetype='application/xml', status=500)
    except Exception as e:
        return Response(f"<error>Server error: {str(e)}</error>", mimetype='application/xml', status=500)

# --- ENDPOINT 1: THE DATA PROXY ---
@app.route('/api/inventory_data')
def get_inventory_data():
    """Reads the CSV and returns JSON for the JS Chart."""
    try:
        if not os.path.exists(INVENTORY_CSV):
            return jsonify({"error": "CSV not found"}), 404
            
        # Read CSV and ensure dates are sorted
        df = pd.read_csv(INVENTORY_CSV)
        df['Date'] = pd.to_datetime(df['Date'])
        df = df.sort_values('Date')
        
        # Convert to dictionary format for JSON
        data = {
            "labels": df['Date'].dt.strftime('%Y-%m-%d').tolist(),
            "registered": df['Registered'].tolist(),
            "eligible": df['Eligible'].tolist(),
            "total": df['Total'].tolist()
        }
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500



# --- ENDPOINT 2: THE DASHBOARD VIEWER ---
@app.route('/inventory_chart')
def inventory_chart_page():
    """Serves a single-page HTML dashboard optimized for iFrame embedding."""
    html_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>COMEX Inventory Live Chart</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            /* OPTIMIZED FOR TERMINAL IFRAME */
            body { font-family: sans-serif; background: transparent; color: #eee; margin: 0; padding: 10px; height: 100vh; box-sizing: border-box; display: flex; flex-direction: column; overflow: hidden; }
            .container { flex: 1; display: flex; flex-direction: column; background: #1e1e1e; padding: 15px; border-radius: 4px; border: 1px solid #27272a; }
            h2 { text-align: center; color: #ffcc00; font-size: 14px; margin: 0 0 10px 0; font-weight: bold; letter-spacing: 1px; }
            .stats { display: flex; justify-content: space-around; margin-bottom: 10px; font-weight: bold; font-size: 11px; }
            .chart-wrapper { flex: 1; position: relative; min-height: 0; }
        </style>
    </head>
    <body>
        <div class="container">
            <h2>🏦 COMEX PHYSICAL INVENTORY HISTORY</h2>
            <div class="stats" id="currentStats">Loading latest data...</div>
            <div class="chart-wrapper">
                <canvas id="inventoryChart"></canvas>
            </div>
        </div>

        <script>
            async function loadChart() {
                const response = await fetch('/api/inventory_data');
                const data = await response.json();

                // Update Stats Header
                const lastIdx = data.labels.length - 1;
                document.getElementById('currentStats').innerHTML = `
                    <span>Total: ${(data.total[lastIdx]/1e6).toFixed(2)}M oz</span>
                    <span style="color: #ff4444">Registered: ${(data.registered[lastIdx]/1e6).toFixed(2)}M oz</span>
                    <span style="color: #44ff44">Eligible: ${(data.eligible[lastIdx]/1e6).toFixed(2)}M oz</span>
                `;

                const ctx = document.getElementById('inventoryChart').getContext('2d');
                new Chart(ctx, {
                    type: 'line',
                    data: {
                        labels: data.labels,
                        datasets: [
                            {
                                label: 'Registered (Sellable)',
                                data: data.registered,
                                borderColor: '#ff4444',
                                backgroundColor: 'rgba(255, 68, 68, 0.1)',
                                fill: true,
                                tension: 0.3,
                                pointRadius: 1
                            },
                            {
                                label: 'Eligible (Vaulted)',
                                data: data.eligible,
                                borderColor: '#44ff44',
                                tension: 0.3,
                                pointRadius: 1
                            },
                            {
                                label: 'Total Inventory',
                                data: data.total,
                                borderColor: '#ffcc00',
                                borderDash: [5, 5],
                                tension: 0.3,
                                pointRadius: 1
                            }
                        ]
                    },
                    options: {
                        devicePixelRatio: 3,    
                        responsive: true,
                        maintainAspectRatio: false, // <--- THE CRITICAL FIX
                        interaction: {
                            mode: 'index',
                            intersect: false,
                        },
                        plugins: {
                            legend: { labels: { color: '#eee', boxWidth: 12, font: {size: 10} } }
                        },
                        scales: {
                            y: { 
                                ticks: { color: '#aaa', font: {size: 10}, callback: (v) => (v/1e6).toFixed(0) + 'M' },
                                grid: { color: '#333' }
                            },
                            x: { 
                                ticks: { color: '#aaa', font: {size: 10}, maxTicksLimit: 10 },
                                grid: { display: false }
                            }
                        }
                    }
                });
            }
            loadChart();
        </script>
    </body>
    </html>
    """
    return render_template_string(html_template)

@app.route('/api/morning', methods=['GET'])
def api_morning():
    ticker = request.args.get('ticker', 'SPY').upper()
    try:
        # Run the scanner script as a subprocess
        # We use a timeout so it doesn't hang the UI forever
        output = subprocess.check_output(
            [PYTHON_BIN, OPTIONS_SCRIPT, ticker, "morning"], 
            text=True, 
            timeout=30
        )
        return jsonify({"status": "success", "ticker": ticker, "data": output})
    except subprocess.CalledProcessError as e:
        # If the script crashes, we return the error as JSON
        return jsonify({
            "status": "error", 
            "message": "Scanner script failed. It might be due to weekend data gaps.",
            "details": str(e.output if hasattr(e, 'output') else e)
        }), 200 # We return 200 so the UI doesn't trigger a browser-level 500 error
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 200

@app.route('/api/evening', methods=['GET'])
def api_evening():
    ticker = request.args.get('ticker', 'SPY').upper()
    try:
        # standardizing to use the subprocess scanner logic
        output = subprocess.check_output(
            [PYTHON_BIN, OPTIONS_SCRIPT, ticker, "evening"], 
            text=True, 
            timeout=30
        )
        return jsonify({"status": "success", "ticker": ticker, "data": output})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 200

# --- HELPER: NATIVE XML WHALE HUNT ---
def execute_xml_whale_hunt(ticker, min_vol_oi=1.0, max_dte=30, strategy="CUSTOM_HUNT"):
    """Fetches option chains and returns a structured XML object."""
    root = ET.Element("whale_hunt", ticker=ticker.upper(), strategy=strategy)
    
    try:
        tk = yf.Ticker(ticker)
        exps = tk.options
        if not exps:
            root.set("error", "No options found")
            return root

        today = datetime.now()
        whale_list = []

        # Iterate through expirations
        for exp in exps:
            exp_date = datetime.strptime(exp, '%Y-%m-%d')
            days_to_exp = (exp_date - today).days
            
            # Apply Max DTE filter
            if max_dte and days_to_exp > int(max_dte):
                continue

            opt = tk.option_chain(exp)
            # Combine calls and puts into one list for processing
            for df, label in [(opt.calls, "CALL"), (opt.puts, "PUT")]:
                if df.empty: continue
                
                # Calculate metrics
                df['premium_spent'] = df['volume'] * df['lastPrice'] * 100
                df['vol_oi_ratio'] = df['volume'] / df['openInterest'].replace(0, 1)
                df['spread'] = df['ask'] - df['bid']

                # Filter: Vol/OI ratio (default >= 1.0)
                # We also add a basic premium floor ($100k) to keep the XML clean
                mask = (df['vol_oi_ratio'] >= float(min_vol_oi)) & (df['premium_spent'] >= 100000)
                whales = df[mask].copy()

                for _, row in whales.iterrows():
                    whale_list.append({
                        "symbol": row['contractSymbol'],
                        "type": label,
                        "expiration": exp,
                        "strike": str(row['strike']),
                        "last_price": f"${row['lastPrice']:.2f}",
                        "bid": f"${row['bid']:.2f}",
                        "ask": f"${row['ask']:.2f}",
                        "spread": f"${row['spread']:.2f}",
                        "volume": str(int(row['volume'])),
                        "open_interest": str(int(row['openInterest'])),
                        "vol_oi_ratio": f"{row['vol_oi_ratio']:.2f}x",
                        "implied_volatility": f"{row['impliedVolatility']*100:.2f}%",
                        "premium_spent": f"${row['premium_spent']:,.2f}",
                        "raw_premium": row['premium_spent'] # For sorting
                    })

        # Sort all found whales by premium spent (Highest first)
        whale_list.sort(key=lambda x: x['raw_premium'], reverse=True)
        root.set("whale_count", str(len(whale_list)))

        # Build the XML tree
        for w in whale_list:
            contract = ET.SubElement(root, "contract")
            for attr in ['symbol', 'type', 'expiration', 'strike', 'last_price', 
                         'bid', 'ask', 'spread', 'volume', 'open_interest', 
                         'vol_oi_ratio', 'implied_volatility', 'premium_spent']:
                contract.set(attr, w[attr])

    except Exception as e:
        root.set("error", str(e))

    return root

# --- THE ROUTE ---
@app.route('/api/custom', methods=['GET'])
def api_custom():
    ticker = request.args.get('ticker', 'SPY').upper()
    try:
        # 1. Parse Parameters
        min_vol_oi = float(request.args.get('min_vol_oi', 1.0))
        max_dte = request.args.get('max_dte', 365) # Default to 1 year if not set

        # 2. Execute the hunt
        xml_root = execute_xml_whale_hunt(ticker, min_vol_oi, max_dte)
        
        # 3. Convert to string and return as XML mimetype
        from xml.dom import minidom
        raw_xml = ET.tostring(xml_root, encoding='utf-8')
        pretty_xml = minidom.parseString(raw_xml).toprettyxml(indent="  ")
        
        return Response(pretty_xml, mimetype='application/xml')

    except Exception as e:
        return Response(f"<error>{str(e)}</error>", mimetype='application/xml', status=500)

@app.route('/', methods=['GET'])
def serve_terminal():
    """Serves the VladHQ Market Terminal UI."""
    return render_template('terminal.html')
    
@app.route('/help', methods=['GET'])
def api_help():
    """Outputs the complete API documentation in XML format."""
    help_xml = """<?xml version="1.0" ?>
<api_documentation>
  <endpoint path="/api/morning">
    <description>8:31 AM LOGIC: Hunts for urgent, short-term directional momentum.</description>
    <defaults max_dte="14 days" min_vol_oi="1.5x" min_premium="$100,000" />
    <parameters>
      <param name="ticker" required="true" type="string" example="SPY" />
    </parameters>
  </endpoint>
  
  <endpoint path="/api/evening">
    <description>2:00 PM LOGIC: Hunts for massive structural positioning and earnings bets.</description>
    <defaults max_dte="ALL" min_vol_oi="1.0x" min_premium="$500,000" />
    <parameters>
      <param name="ticker" required="true" type="string" example="NVDA" />
    </parameters>
  </endpoint>
  
  <endpoint path="/api/custom">
    <description>CUSTOM LOGIC: Dynamic scanner allowing user-defined overrides.</description>
    <parameters>
      <param name="ticker" required="true" type="string" example="TSLA" />
      <param name="min_vol_oi" required="false" type="float" default="1.0" example="3.5" description="Minimum Volume to Open Interest ratio" />
      <param name="min_premium" required="false" type="float" default="100000" example="1000000" description="Minimum estimated dollars spent" />
      <param name="max_dte" required="false" type="int" default="None" example="5" description="Maximum days to expiration (Leave blank for ALL)" />
    </parameters>
  </endpoint>

  <endpoint path="/run">
    <description>SYSTEM: Triggers the local run_dashboard.command script on the Mac and returns the execution timestamp.</description>
    <parameters />
  </endpoint>

  <endpoint path="/dump">
    <description>DATA: Automatically locates the most recent CME_Data folder and dumps the tactical ruling and volume dashboard text files.</description>
    <parameters />
  </endpoint>

  <endpoint path="/vmri">
    <description>MACRO: Extracts the latest Vlad Macro Risk Index (VMRI) score, including live calculations, formulas, and the mechanics breakdown from the latest tactical ruling.</description>
    <parameters />
  </endpoint>

  <endpoint path="/api/inventory_data">
    <description>DATA PROXY: Reads the master COMEX inventory history CSV and returns a JSON payload of Registered, Eligible, and Total volumes for time-series analysis.</description>
    <parameters />
  </endpoint>

  <endpoint path="/inventory_chart">
    <description>VISUAL: Serves a full-screen, high-contrast interactive dashboard using Chart.js to visualize physical COMEX silver inventory trends and vault drains.</description>
    <parameters />
  </endpoint>
</api_documentation>

  <endpoint path="/api/vmri_history">
    <description>HISTORICAL: Dumps the last 500 records from the macro ledger as JSON for external analysis.</description>
    <parameters />
  </endpoint>

  <endpoint path="/vmri_chart">
    <description>VISUAL: A "Mannarino-Style" historical trend chart for the VMRI. Shows risk escalation over time with color-coded threat zones.</description>
    <parameters />
  </endpoint>"""
    
    return Response(help_xml, mimetype='application/xml')

@app.route('/run', methods=['GET'])
def run_dashboard():
    """Executes the local dashboard command script and returns the exact timestamp."""
    script_path = RUN_COMMAND
    
    # Verify the file actually exists before trying to run it
    if not os.path.exists(script_path):
        return Response("<error>Script not found at specified path.</error>", mimetype='application/xml', status=404)

    try:
        # Run the script. capture_output=True hides the terminal spam from the Flask console.
        # check=True forces Python to throw an error if the bash script fails or crashes.
        subprocess.run(["bash", script_path], check=True, capture_output=True, text=True)
        
        # Grab the exact time down to the second
        exact_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # Build the XML success response
        root = ET.Element("execution_result", status="SUCCESS", timestamp=exact_time)
        msg = ET.SubElement(root, "message")
        msg.text = "Dashboard run initiated and completed successfully."
        
        xml_str = minidom.parseString(ET.tostring(root, encoding='utf-8')).toprettyxml(indent="  ")
        return Response(xml_str, mimetype='application/xml')

    except subprocess.CalledProcessError as e:
        # If the bash script crashes, this catches it and outputs the actual bash error
        return Response(f"<error>Script crashed during execution: {e.stderr}</error>", mimetype='application/xml', status=500)
    except Exception as e:
        return Response(f"<error>System Error: {str(e)}</error>", mimetype='application/xml', status=500)

@app.route('/api/dump', methods=['GET'])
@app.route('/dump', methods=['GET'])
def dump_data():
    """Finds the most recent CME_Data folder and intelligently merges XML files."""
    base_dir = str(_DATA_DIR) + "/"
    
    if not os.path.exists(base_dir):
        return Response("<error>CME_Data directory not found.</error>", mimetype='application/xml', status=404)

    # 1. Locate the latest folder
    subdirs = [os.path.join(base_dir, d) for d in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, d))]
    if not subdirs:
        return Response("<error>No folders found.</error>", mimetype='application/xml', status=404)
    subdirs.sort(key=lambda x: os.path.getmtime(x), reverse=True)
    latest_folder = subdirs[0]

    # 2. Setup paths
    tactical_path = os.path.join(latest_folder, "tactical_ruling.txt")
    volume_path = os.path.join(latest_folder, "volume_dashboard.txt")

    # 3. Create Master Container
    root = ET.Element("cme_data_dump", source_folder=os.path.basename(latest_folder), timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    # Helper to parse and strip headers
    def get_parsed_xml(path):
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
                clean = re.sub(r'<\?xml[^>]*\?>', '', content).strip()
                try:
                    return ET.fromstring(clean)
                except: return None
        return None

    # 4. INTELLECTUALLY MERGE
    tac_xml = get_parsed_xml(tactical_path)
    vol_xml = get_parsed_xml(volume_path)

    # If the tactical ruling exists standalone, add it first
    if tac_xml is not None:
        root.append(tac_xml)

    if vol_xml is not None:
        # CHECK: Is there a redundant tactical ruling inside the dashboard?
        # If so, remove it from the dashboard block so we don't have double data
        redundant_node = vol_xml.find("tactical_ruling")
        if redundant_node is not None and tac_xml is not None:
            vol_xml.remove(redundant_node)
            
        root.append(vol_xml)

    # 5. Generate Pretty Output
    raw_xml = ET.tostring(root, encoding='utf-8')
    xml_str = minidom.parseString(raw_xml).toprettyxml(indent="  ")
    
    # Remove empty lines for a tighter output
    xml_str = os.linesep.join([s for s in xml_str.splitlines() if s.strip()])

    return Response(xml_str, mimetype='application/xml')

@app.route('/vmri', methods=['GET'])
def get_vmri():
    """Extracts the latest VMRI score and outputs it with full documentation and formulas."""
    base_dir = str(_DATA_DIR) + "/"
    
    if not os.path.exists(base_dir):
        return Response("<error>CME_Data directory not found.</error>", mimetype='application/xml', status=404)

    # Find the newest folder
    subdirs = [os.path.join(base_dir, d) for d in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, d))]
    if not subdirs:
        return Response("<error>No daily folders found.</error>", mimetype='application/xml', status=404)

    subdirs.sort(key=lambda x: os.path.getmtime(x), reverse=True)

    latest_folder = None
    tactical_content = ""

    # Hunt for the tactical ruling file
    for folder in subdirs:
        tactical_path = os.path.join(folder, "tactical_ruling.txt")
        if os.path.exists(tactical_path):
            latest_folder = folder
            with open(tactical_path, 'r', encoding='utf-8') as f:
                tactical_content = f.read()
            break

    if not tactical_content:
        return Response("<error>tactical_ruling.txt not found in recent folders.</error>", mimetype='application/xml', status=404)

    clean_content = re.sub(r'<\?xml[^>]*\?>', '', tactical_content).strip()
    
    try:
        tactical_tree = ET.fromstring(clean_content)
        vmri_node = tactical_tree.find(".//VLAD_MACRO_RISK_INDEX")
        
        if vmri_node is None:
            return Response("<error>VMRI data not found inside the latest tactical ruling.</error>", mimetype='application/xml', status=404)

        root = ET.Element("vmri_report", timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'), source_folder=os.path.basename(latest_folder))
        
        # 1. Attach Live Data
        live_data = ET.SubElement(root, "live_calculation")
        live_data.append(vmri_node)

        # 2. Attach Top-Level Formula
        formula_node = ET.SubElement(root, "master_formula")
        formula_node.text = "VMRI Score = Base Stress * Credit Multiplier * Volatility Premium"
        
        # 3. Attach Documentation
        doc_node = ET.SubElement(root, "documentation")
        
        desc = ET.SubElement(doc_node, "description")
        desc.text = "The Vlad Macro Risk Index (VMRI) aggregates fixed income stress, credit spreads, and equity volatility into a single numerical threat level to dictate portfolio hedging aggression."
        
        # --- NEW: Mechanics Breakdown with Formulas ---
        mechanics_doc = ET.SubElement(doc_node, "mechanics_breakdown")
        
        # Base Stress
        bs = ET.SubElement(mechanics_doc, "metric", name="Base Stress")
        bs_desc = ET.SubElement(bs, "description")
        bs_desc.text = "The foundational risk score derived from interest rate volatility, 10-Year Treasury yields, and systemic liquidity."
        bs_form = ET.SubElement(bs, "formula")
        bs_form.text = "(DXY * 1.5) + (10Y Yield * 15) + (150 - ZN Futures) + (Reverse Repo * 10)"
        
        # Credit Multiplier
        cm = ET.SubElement(mechanics_doc, "metric", name="Credit Multiplier")
        cm_desc = ET.SubElement(cm, "description")
        cm_desc.text = "A scaling factor based on High Yield OAS spreads. Values < 1.0 mean credit is healthy and dampens risk. Values > 1.0 indicate widening credit spreads, amplifying the systemic threat."
        cm_form = ET.SubElement(cm, "formula")
        cm_form.text = "Current HY OAS / 4.00"
        cm_base = ET.SubElement(cm, "baseline")
        cm_base.text = "4.00%"
        
        # Volatility Premium
        vp = ET.SubElement(mechanics_doc, "metric", name="Volatility Premium")
        vp_desc = ET.SubElement(vp, "description")
        vp_desc.text = "An accelerator based on equity derivatives (VIX). Values > 1.0 mean options markets are pricing in severe near-term turbulence, driving up the final score."
        vp_form = ET.SubElement(vp, "formula")
        vp_form.text = "Current VIX / 20.00"
        vp_base = ET.SubElement(vp, "baseline")
        vp_base.text = "20.00"
        
        # Ranges
        ranges = ET.SubElement(doc_node, "ranges")
        ET.SubElement(ranges, "level", range="0 - 150", status="RISK ON", action="Maximize long exposure. Volatility is suppressed.")
        ET.SubElement(ranges, "level", range="150 - 250", status="BASELINE", action="Normal market conditions. Standard position sizing.")
        ET.SubElement(ranges, "level", range="250 - 350", status="ELEVATED RISK", action="Hedge triggers active. Reduce beta, increase cash.")
        ET.SubElement(ranges, "level", range="350+", status="SEVERE STRESS", action="Liquidity event probable. Maximum defensive posture.")

        # Generate pretty XML
        xml_str = minidom.parseString(ET.tostring(root, encoding='utf-8')).toprettyxml(indent="  ")
        xml_str = os.linesep.join([s for s in xml_str.splitlines() if s.strip()]) 

        return Response(xml_str, mimetype='application/xml')

    except Exception as e:
        return Response(f"<error>Failed to parse VMRI data: {str(e)}</error>", mimetype='application/xml', status=500)

@app.route('/api/macro_news', methods=['GET'])
def api_macro_news():
    """Fetches general macroeconomic news via public RSS without an API key."""
    try:
        # Switched to Yahoo Finance (Much more scraper-friendly than CNBC)
        rss_url = "https://finance.yahoo.com/news/rss"
        
        # A more robust set of headers to mimic a real browser
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5'
        }
        
        response = requests.get(rss_url, headers=headers, timeout=5)
        
        if response.status_code != 200:
            return Response(f"<error>Failed to fetch feed. HTTP Status: {response.status_code}</error>", mimetype='application/xml', status=502)
            
        # Parse the XML feed
        feed_tree = ET.fromstring(response.content)
        items = feed_tree.findall(".//item")[:10] # Grab top 10 headlines
        
        # Build our custom XML tree
        root = ET.Element("macro_news", timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'), source="Yahoo_Finance")
        
        for item in items:
            title = item.findtext("title", "No Title")
            pub_date = item.findtext("pubDate", "Unknown Date")
            link = item.findtext("link", "No Link")
            
            # NEW: Sentiment Analysis
            sentiment_score = analyzer.polarity_scores(title)['compound']
            
            article = ET.SubElement(root, "article")
            article.set("published", pub_date.replace(" GMT", "").replace(" +0000", ""))
            article.set("title", title.strip())
            article.set("link", link.strip())
            article.set("sentiment", str(round(sentiment_score, 2)))
            
        xml_str = minidom.parseString(ET.tostring(root, encoding='utf-8')).toprettyxml(indent="  ")
        xml_str = os.linesep.join([s for s in xml_str.splitlines() if s.strip()]) # Clean blank lines
        
        return Response(xml_str, mimetype='application/xml')

    except Exception as e:
        return Response(f"<error>System Error: {str(e)}</error>", mimetype='application/xml', status=500)

# ==========================================
# --- INSTITUTIONAL FLOW HISTORY API ---
# ==========================================

INSTITUTIONAL_LEDGER_CSV = os.path.join(DATA_DIR, "equities_darkpool_gex_ledger.csv")

@app.route('/api/institutional_history')
def get_institutional_history():
    """Returns time-series data from the institutional scanner ledger (SPY + SLV dark pool + GEX)."""
    ticker = request.args.get('ticker', 'SLV').upper()
    limit = int(request.args.get('limit', 100))
    
    try:
        if not os.path.exists(INSTITUTIONAL_LEDGER_CSV):
            return jsonify({"status": "error", "message": "Institutional ledger not found."}), 404
        
        df = data_cache.get_csv(INSTITUTIONAL_LEDGER_CSV)
        
        if df.empty:
            return jsonify({"status": "error", "message": "Ledger is empty."}), 404
        
        # Filter by ticker (SPY or SLV)
        df = df[df['Ticker'].str.upper() == ticker].copy()
        
        if df.empty:
            return jsonify({"status": "error", "message": f"No data found for {ticker}."}), 404
        
        df = df.tail(limit)
        
        # Clean NaN values for JSON serialization
        df = df.astype(object).where(pd.notnull(df), None)
        
        # Build the payload
        payload = {
            "ticker": ticker,
            "labels": df['Date'].tolist(),
            "spot_price": df['Spot_Price'].tolist(),
            "dp_sentiment": df['DP_Sentiment'].tolist(),
            "dp_total_vol": df['DP_Total_Vol'].tolist(),
            "dp_notional": df['DP_Notional_USD'].tolist(),
            "dp_largest_block": df['DP_Largest_Block'].tolist(),
            "dp_vwap": df['DP_VWAP'].tolist(),
            "dp_bull_vol": df['DP_Bull_Vol'].tolist(),
            "dp_bear_vol": df['DP_Bear_Vol'].tolist(),
            "gex_call_wall": df['GEX_Call_Wall'].tolist(),
            "gex_put_wall": df['GEX_Put_Wall'].tolist(),
            "gex_zero_gamma": df['GEX_Zero_Gamma'].tolist()
        }
        
        return jsonify({"status": "success", "data": payload})
    
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# --- MACRO CALENDAR API ---
# ==========================================

@app.route('/api/macro_calendar')
def get_macro_calendar():
    """Extracts upcoming macro catalyst events from the latest tactical_ruling.txt XML."""
    try:
        base_dir = str(_DATA_DIR) + "/"
        
        if not os.path.exists(base_dir):
            return jsonify({"status": "error", "message": "CME_Data directory not found."}), 404
        
        # Find the latest folder with a tactical_ruling.txt
        subdirs = [os.path.join(base_dir, d) for d in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, d))]
        subdirs.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        
        tactical_content = None
        for folder in subdirs:
            tac_path = os.path.join(folder, "tactical_ruling.txt")
            if os.path.exists(tac_path):
                with open(tac_path, 'r', encoding='utf-8') as f:
                    tactical_content = f.read()
                break
        
        if not tactical_content:
            return jsonify({"status": "error", "message": "No tactical_ruling.txt found."}), 404
        
        # Parse the XML
        clean_content = re.sub(r'<\?xml[^>]*\?>', '', tactical_content).strip()
        root = ET.fromstring(clean_content)
        
        events = []
        calendar_node = root.find(".//upcoming_macro_events")
        
        if calendar_node is not None:
            for event in calendar_node.findall("event"):
                events.append({
                    "date": event.get("date", ""),
                    "time": event.get("time", ""),
                    "impact": event.get("impact", ""),
                    "title": event.get("title", ""),
                    "forecast": event.get("forecast", ""),
                    "previous": event.get("previous", "")
                })
        
        return jsonify({"status": "success", "events": events})
    
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# --- FULL MACRO LEDGER API ---
# ==========================================

@app.route('/api/macro_ledger_full')
def get_macro_ledger_full():
    """Returns all 25 columns from the macro master ledger for full overlay charting."""
    limit = int(request.args.get('limit', 200))
    
    try:
        if not os.path.exists(LEDGER_CSV):
            return jsonify({"status": "error", "message": "Macro ledger not found."}), 404
        
        df = data_cache.get_csv(LEDGER_CSV)
        df = df.dropna(how='all')
        df = df.dropna(subset=['Datetime'])
        df['Datetime'] = pd.to_datetime(df['Datetime'], format='mixed')
        df = df.dropna(subset=['Datetime'])
        df = df.sort_values('Datetime').tail(limit)
        
        labels = df['Datetime'].dt.strftime('%Y-%m-%d %H:%M').tolist()
        
        def safe_list(col_name):
            """Convert a column to a clean list, replacing NaN/inf with None."""
            if col_name not in df.columns:
                return [None] * len(df)
            vals = []
            for val in df[col_name].tolist():
                if pd.isna(val) or val in ["NaN", "nan", "None", ""]:
                    vals.append(None)
                else:
                    try:
                        f = float(val)
                        if math.isnan(f) or math.isinf(f):
                            vals.append(None)
                        else:
                            vals.append(f)
                    except (ValueError, TypeError):
                        vals.append(None)
            return vals
        
        payload = {
            "labels": labels,
            "vmri_score": safe_list("VMRI_Score"),
            "threat_tier": df.get("Threat_Tier", pd.Series(dtype=str)).fillna("N/A").tolist(),
            "dxy": safe_list("DXY"),
            "dxy_change": safe_list("DXY_Change"),
            "ten_y_yield": safe_list("10Y_Yield"),
            "zn_futures": safe_list("ZN_Futures"),
            "high_yield_oas": safe_list("High_Yield_OAS"),
            "vix": safe_list("VIX"),
            "vix_change": safe_list("VIX_Change"),
            "wti_crude": safe_list("WTI_Crude"),
            "brent_crude": safe_list("Brent_Crude"),
            "gold_price": safe_list("Gold_Price"),
            "gold_silver_ratio": safe_list("Gold_Silver_Ratio"),
            "shfe_silver_usd": safe_list("SHFE_Silver_USD"),
            "comex_silver": safe_list("COMEX_Silver"),
            "shfe_premium": safe_list("SHFE_Premium"),
            "gex": safe_list("GEX"),
            "dix": safe_list("DIX"),
            "reverse_repo_bn": safe_list("Reverse_Repo_BN"),
            "fed_balance_sheet_bn": safe_list("Fed_Balance_Sheet_BN"),
            "retail_silver_cheapest": safe_list("Retail_Silver_Cheapest"),
            "retail_silver_avg": safe_list("Retail_Silver_Avg"),
            "silver_oi": safe_list("Silver_OI"),
            "paper_physical_ratio": safe_list("Paper_Physical_Ratio")
        }
        
        return jsonify({"status": "success", "data": payload})
    
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/time_arbitrage')
def get_time_arbitrage():
    ticker_symbol = request.args.get('ticker', 'SPY').upper()
    
    try:
        # 1. Get Live Data for Oscillator
        vix_data = yf.Ticker("^VIX").history(period='1d')
        current_vix = vix_data['Close'].iloc[-1] if not vix_data.empty else 20.0
        
        # Pull latest GEX/DIX from Macro Ledger
        df_macro = data_cache.get_csv(LEDGER_CSV)
        latest_macro = df_macro.iloc[-1]
        current_gex = latest_macro.get('GEX', 0)
        current_dix = latest_macro.get('DIX', 0)
        
        z_score = quant.calculate_z_score_oscillator({
            'vix': current_vix,
            'gex': current_gex,
            'dix': current_dix
        })
        
        # 2. Gamma State (from Institutional Ledger)
        INST_LEDGER = str(_DATA_DIR / "equities_darkpool_gex_ledger.csv")
        zero_gamma = 0
        spot_price = 0
        
        if os.path.exists(INST_LEDGER):
            df_inst = data_cache.get_csv(INST_LEDGER)
            ticker_inst = df_inst[df_inst['Ticker'] == ticker_symbol].tail(1)
            if not ticker_inst.empty:
                inst_row = ticker_inst.iloc[0]
                spot_price = float(inst_row['Spot_Price'])
                zero_gamma = float(inst_row['GEX_Zero_Gamma'])
        
        if spot_price == 0:
            ticker = yf.Ticker(ticker_symbol)
            spot_price = ticker.info.get('regularMarketPrice') or ticker.history(period='1d')['Close'].iloc[-1]
            zero_gamma = spot_price * 0.995 
        
        gamma_state = quant.get_gamma_state(spot_price, zero_gamma)
        
        # 2b. Fetch Chain for Analytics & Filter 0DTE
        ticker = yf.Ticker(ticker_symbol)
        expirations = ticker.options
        if not expirations:
            return jsonify({"status": "error", "message": "No options available for this ticker."})
        
        # Filter out 0DTE
        exp = expirations[0]
        for potential_exp in expirations:
            days = (datetime.strptime(potential_exp, '%Y-%m-%d') - datetime.now()).days
            if days > 0:
                exp = potential_exp
                break
                
        chain = ticker.option_chain(exp)
        
        # Calculate ATM IV and Realized Volatility for IV/HV Spread
        try:
            atm_idx = (chain.calls['strike'] - spot_price).abs().idxmin()
            atm_iv = chain.calls.loc[atm_idx, 'impliedVolatility']
        except:
            atm_iv = 0.20
            
        realized_vol = quant.calculate_realized_volatility(ticker_symbol)
        iv_hv_spread = {
            'realized_volatility_20d': float(realized_vol),
            'atm_implied_volatility': float(atm_iv)
        }
        
        # Term Structure Caching
        term_structure = data_cache.get(f"{ticker_symbol}_term_structure")
        if not term_structure:
            term_structure = []
            targets = [7, 30, 90, 180]
            for target in targets:
                try:
                    closest_exp = min(expirations, key=lambda x: abs((datetime.strptime(x, '%Y-%m-%d') - datetime.now()).days - target))
                    chain_t = ticker.option_chain(closest_exp)
                    idx = (chain_t.calls['strike'] - spot_price).abs().idxmin()
                    t_iv = chain_t.calls.loc[idx, 'impliedVolatility']
                    days_t = max(1, (datetime.strptime(closest_exp, '%Y-%m-%d') - datetime.now()).days)
                    term_structure.append({'days': days_t, 'iv': float(t_iv)})
                except:
                    pass
            if term_structure:
                data_cache.set(f"{ticker_symbol}_term_structure", term_structure)
        
        # 3. IV Bleed & Historical Baseline
        hist_iv_baseline = realized_vol # Use the exact 20-day trailing realized volatility

        iv_bleed = []
        strikes_with_iv = []
        
        days_to_exp = (datetime.strptime(exp, '%Y-%m-%d') - datetime.now()).days
        if days_to_exp <= 0: days_to_exp = 1
        
        r_rate = quant.get_risk_free_rate()
        aggregate_vanna = 0.0
        aggregate_charm = 0.0
        vanna_profile = []
        
        for i in range(min(15, len(chain.calls))):
            row = chain.calls.iloc[i]
            live_iv = row['impliedVolatility']
            strike = float(row['strike'])
            
            # Use real baseline instead of random mock
            hist_iv = float(hist_iv_baseline)
            
            iv_bleed.append({
                'strike': strike,
                'live_iv': float(live_iv),
                'hist_iv': float(hist_iv),
                'bleed': float((live_iv - hist_iv) / hist_iv if hist_iv > 0 else 0)
            })
            
            strikes_with_iv.append({'strike': strike, 'iv': live_iv})
            
            # Second-order Greeks
            if live_iv > 0:
                vanna = quant.calculate_vanna(spot_price, strike, days_to_exp, r_rate, live_iv, is_call=True)
                charm = quant.calculate_charm(spot_price, strike, days_to_exp, r_rate, live_iv, is_call=True)
                # Weight by open interest, default to 1 if missing
                oi = row['openInterest'] if pd.notna(row['openInterest']) and row['openInterest'] > 0 else 1
                aggregate_vanna += vanna * oi
                aggregate_charm += charm * oi
                vanna_profile.append({'strike': strike, 'vanna': float(vanna * oi), 'charm': float(charm * oi)})
            
        # 4. Probabilities (Skew-Adjusted & Time-Accurate)
        prob_matrix = quant.calculate_strike_probabilities(spot_price, strikes_with_iv[:5], days_list=[3, 5, 7])
            
        dealer_trapdoor = {
            "vanna_exposure": float(aggregate_vanna),
            "charm_exposure": float(aggregate_charm),
            "vanna_profile": vanna_profile
        }

        return jsonify({
            "status": "success",
            "data": {
                "z_score": z_score,
                "gamma_state": gamma_state,
                "dealer_trapdoor": dealer_trapdoor,
                "iv_bleed": iv_bleed,
                "probabilities": prob_matrix,
                "term_structure": term_structure,
                "iv_hv_spread": iv_hv_spread
            }
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/macro_direction')
def get_macro_direction():
    """Consolidated endpoint for Tab 1 (Macro Direction) data snapshots."""
    try:
        # Pull latest from ledger
        df = data_cache.get_csv(LEDGER_CSV).tail(1)
        if df.empty:
            return jsonify({"status": "error", "message": "No ledger data."})
            
        latest = df.iloc[0].to_dict()
        
        return jsonify({
            "status": "success",
            "data": {
                "vmri": latest.get('VMRI_Score'),
                "sentiment_bias": "NEUTRAL"
            }
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


# ==========================================
# OPTION EXPLORER & ANALYTICS API
# ==========================================

@app.route('/api/option_chain', methods=['GET'])
def get_option_chain():
    """Returns all expirations and strikes for a ticker."""
    ticker_symbol = request.args.get('ticker', 'SPY').upper()
    expiration = request.args.get('expiration', None)
    try:
        tk = yf.Ticker(ticker_symbol)
        spot_info = tk.history(period='1d')
        if spot_info.empty:
            return jsonify({"status": "error", "message": "Could not fetch spot price."})
        spot = float(spot_info['Close'].iloc[-1])

        expirations = list(tk.options)
        if not expirations:
            return jsonify({"status": "error", "message": "No options found for this ticker."})

        if expiration and expiration in expirations:
            selected_exp = expiration
        else:
            # Default: first non-0DTE
            selected_exp = expirations[0]
            for e in expirations:
                if (datetime.strptime(e, '%Y-%m-%d') - datetime.now()).days > 0:
                    selected_exp = e
                    break

        chain = tk.option_chain(selected_exp)
        days_to_exp = max(1, (datetime.strptime(selected_exp, '%Y-%m-%d') - datetime.now()).days)

        def format_contracts(df, option_type):
            rows = []
            for _, row in df.iterrows():
                iv = row.get('impliedVolatility', 0)
                last = row.get('lastPrice', 0)
                bid = row.get('bid', 0)
                ask = row.get('ask', 0)
                oi = row.get('openInterest', 0)
                volume = row.get('volume', 0)
                rows.append({
                    'strike': float(row['strike']),
                    'type': option_type,
                    'last': float(last) if pd.notna(last) else 0,
                    'bid': float(bid) if pd.notna(bid) else 0,
                    'ask': float(ask) if pd.notna(ask) else 0,
                    'iv': float(iv) if pd.notna(iv) else 0,
                    'oi': int(oi) if pd.notna(oi) else 0,
                    'volume': int(volume) if pd.notna(volume) else 0,
                })
            return rows

        calls = format_contracts(chain.calls, 'call')
        puts = format_contracts(chain.puts, 'put')

        return jsonify({
            "status": "success",
            "data": {
                "ticker": ticker_symbol,
                "spot": spot,
                "expirations": expirations[:24],  # limit to next ~2 years
                "selected_expiration": selected_exp,
                "days_to_exp": days_to_exp,
                "calls": calls,
                "puts": puts,
            }
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route('/api/option_calc', methods=['GET'])
def calculate_option():
    """Runs full Black-Scholes analytics on a specific contract."""
    try:
        ticker_symbol = request.args.get('ticker', 'SPY').upper()
        strike = float(request.args.get('strike', 0))
        expiration = request.args.get('expiration', '')
        option_type = request.args.get('type', 'call').lower()
        market_price = float(request.args.get('market_price', 0))

        tk = yf.Ticker(ticker_symbol)
        spot_info = tk.history(period='1d')
        if spot_info.empty:
            return jsonify({"status": "error", "message": "Could not fetch spot price."})
        spot = float(spot_info['Close'].iloc[-1])

        days = max(1, (datetime.strptime(expiration, '%Y-%m-%d') - datetime.now()).days)
        r = quant.get_risk_free_rate()

        # Pull IV from the live chain for accuracy
        iv = 0.20
        try:
            chain = tk.option_chain(expiration)
            df = chain.calls if option_type == 'call' else chain.puts
            match = df[df['strike'] == strike]
            if not match.empty:
                iv_raw = match.iloc[0]['impliedVolatility']
                if pd.notna(iv_raw) and iv_raw > 0:
                    iv = float(iv_raw)
                mp = match.iloc[0]['lastPrice']
                if market_price == 0 and pd.notna(mp):
                    market_price = float(mp)
        except Exception:
            pass

        analytics = quant.calculate_option_analytics(
            spot=spot, strike=strike, days=days,
            r=r, vol=iv, option_type=option_type,
            market_price=market_price
        )
        analytics['spot'] = round(spot, 2)
        analytics['strike'] = strike
        analytics['expiration'] = expiration
        analytics['days_to_exp'] = days
        analytics['option_type'] = option_type.upper()

        return jsonify({"status": "success", "data": analytics})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

# ==========================================
# --- TRACKED ENGINE POSITIONS (v2_trade_signals) ---
# ==========================================
from config import DB_PATH as _V2_DB_PATH
from core import lake as _v2lake, positions as _v2pos


def _cached_underlying(sym):
    key = f"pos_quote:{sym}"
    hit = data_cache.get(key)
    if hit is not None:
        return hit
    try:
        closes = yf.Ticker(sym).history(period="5d", interval="1m")["Close"].dropna()
        if closes.empty:
            closes = yf.Ticker(sym).history(period="7d")["Close"].dropna()
        px = float(closes.iloc[-1]) if not closes.empty else None
    except Exception:
        px = None
    data_cache.set(key, px)
    return px


def _cached_chain(sym, exp):
    key = f"pos_chain:{sym}:{exp}"
    hit = data_cache.get(key)
    if hit is not None:
        return hit
    try:
        ch = yf.Ticker(sym).option_chain(exp)
        val = {"calls": ch.calls, "puts": ch.puts}
    except Exception:
        val = {}
    data_cache.set(key, val)
    return val


def _cached_daily_closes(sym):
    key = f"pos_daily:{sym}"
    hit = data_cache.get(key)
    if hit is not None:
        return hit
    try:
        s = yf.Ticker(sym).history(period="2y")["Close"].dropna()
        s.index = s.index.tz_localize(None).normalize()
    except Exception:
        s = pd.Series(dtype=float)
    data_cache.set(key, s)
    return s


@app.route('/api/positions', methods=['GET'])
def api_positions():
    conn = _v2lake.connect(_V2_DB_PATH)
    try:
        rows = _v2pos.list_live(conn, _cached_underlying, _cached_chain, _cached_daily_closes)
    finally:
        conn.close()
    return jsonify({"status": "success", "data": rows, "as_of": datetime.now().isoformat(timespec="seconds")})


@app.route('/api/positions/<int:signal_id>/star', methods=['POST'])
def api_position_star(signal_id):
    conn = _v2lake.connect(_V2_DB_PATH)
    try:
        starred = _v2pos.toggle_star(conn, signal_id)
    finally:
        conn.close()
    if starred is None:
        return jsonify({"status": "error", "message": "not found"}), 404
    return jsonify({"status": "success", "starred": starred})


@app.route('/api/positions/<int:signal_id>', methods=['DELETE'])
def api_position_delete(signal_id):
    conn = _v2lake.connect(_V2_DB_PATH)
    try:
        ok = _v2pos.stop_tracking(conn, signal_id)
    finally:
        conn.close()
    return (jsonify({"status": "success"}) if ok else (jsonify({"status": "error", "message": "not found"}), 404))



# ==========================================
# --- FORECAST LAB (reads the latest committed v2 snapshot) ---
# ==========================================
from core import forecast as _v2fc


def _latest_forecast():
    conn = _v2lake.connect(_V2_DB_PATH)
    try:
        row = conn.execute("SELECT run_id FROM v2_latest_snapshot").fetchone()
        if not row:
            return None, None, conn
        key = f"fc_snapshot:{row['run_id']}"
        hit = data_cache.get(key)
        if hit is None:
            ctx = _v2lake.load_snapshot(conn, row["run_id"])
            hit = {"run": ctx["run"], "forecast": ctx.get("forecast"), "refining": ctx.get("refining")}
            data_cache.set(key, hit)
        return hit["run"], dict(hit["forecast"] or {}, _refining=hit.get("refining")), conn
    except Exception:
        conn.close()
        raise


@app.route('/api/forecast', methods=['GET'])
def api_forecast():
    ticker = request.args.get('ticker', 'SPY').upper()
    if ticker not in ("SPY", "SLV"):
        return jsonify({"status": "error", "message": "ticker must be SPY or SLV"}), 400
    run, fc, conn = _latest_forecast()
    try:
        if not fc or fc.get("status") == "error":
            return jsonify({"status": "error", "message": (fc or {}).get("reason") or "no Forecast Lab data yet: run the pipeline",
                            "run": run}), 404
        score = _v2fc.scorecard(conn, _cached_daily_closes, ticker)
    finally:
        conn.close()
    return jsonify({"status": "success", "ticker": ticker, "run": run, "horizons": fc.get("horizons"),
                    "data": fc.get(ticker), "consensus": (fc.get("consensus") or {}).get(ticker),
                    "positioning": fc.get("positioning"), "silver_fair_value": fc.get("silver_fair_value"),
                    "macro_regime": fc.get("macro_regime"), "source_errors": fc.get("source_errors"),
                    "refining": fc.get("_refining"),
                    "scorecard": score})


_EIA_HISTORY_CACHE = {}


@app.route('/api/eia_history', methods=['GET'])
def api_eia_history():
    """Full weekly history behind Forecast Lab card 11, built from the EIA files the pipeline already captured."""
    from core import refining as _v2rf, sources as _v2src
    keys = sorted({k for k, _ in _v2rf.INVENTORY_LINES} | set(_v2rf.DEMAND_FOR.values()))
    conn = _v2lake.connect(_V2_DB_PATH, readonly=True)
    try:
        rows = {}
        for key in keys:
            row = conn.execute("""SELECT payload_id FROM v2_payloads WHERE kind = 'eia_weekly' AND request_json LIKE ?
                                  ORDER BY fetched_at DESC LIMIT 1""", (f'%"series": "{_v2rf.EIA_SERIES[key]}"%',)).fetchone()
            if row:
                rows[key] = row["payload_id"]
        stamp = tuple(sorted(rows.items()))
        if _EIA_HISTORY_CACHE.get("stamp") != stamp:
            series = {}
            for key, pid in rows.items():
                _, content = _v2lake.load_payload(conn, pid)
                if content:
                    series[key] = _v2src._eia_parse(content)
            _EIA_HISTORY_CACHE.update(stamp=stamp, data=_v2rf.inventory_history(series))
        hist = _EIA_HISTORY_CACHE["data"]
    except Exception as e:
        return jsonify({"status": "error", "message": f"{type(e).__name__}: {e}"}), 500
    finally:
        conn.close()
    if hist.get("status") != "fresh":
        return jsonify({"status": "error", "message": hist.get("reason") or "no EIA data yet: run the pipeline"}), 404
    return jsonify(dict(hist, status="success"))


if __name__ == "__main__":
    # Port 8080 unless OPTIONS_WHALE_PORT is set in .env (per-machine, never committed)
    app.run(host='0.0.0.0', port=int(os.environ.get("OPTIONS_WHALE_PORT", "8080")))