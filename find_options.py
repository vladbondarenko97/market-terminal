import sys
import yfinance as yf
import pandas as pd
from datetime import datetime
import xml.etree.ElementTree as ET
from xml.dom import minidom
import warnings

#How to use:
# python find_options.py {ticker} {expiration, MM/DD/YYY}

# Suppress pandas FutureWarnings for clean CLI output
warnings.simplefilter(action='ignore', category=FutureWarning)

def print_error_xml(message):
    root = ET.Element("error")
    root.text = message
    print(minidom.parseString(ET.tostring(root)).toprettyxml(indent="  "))
    sys.exit(1)

def build_contract_xml(parent, tag_name, contract):
    """Helper to safely build an XML node for a contract."""
    node = ET.SubElement(parent, tag_name)
    node.set("symbol", str(contract.get('contractSymbol', 'N/A')))
    node.set("expiration", str(contract.get('expiration', 'N/A')))
    node.set("strike", str(contract.get('strike', '0')))
    node.set("volume", str(int(contract.get('volume', 0))))
    node.set("open_interest", str(int(contract.get('openInterest', 0))))
    node.set("last_price", f"{contract.get('lastPrice', 0.0):.2f}")
    
    iv = contract.get('impliedVolatility', 0.0)
    node.set("implied_volatility", f"{iv * 100:.2f}%")
    return node

def main():
    if len(sys.argv) < 2:
        print_error_xml("Usage: python find_options.py [TICKER] [MM/DD/YYYY - Optional]")

    ticker_symbol = sys.argv[1].upper()
    target_exp_input = sys.argv[2] if len(sys.argv) > 2 else None

    # 1. Parse Date if provided
    target_exp_yf = None
    if target_exp_input:
        try:
            # Convert MM/DD/YYYY to YYYY-MM-DD for Yahoo Finance
            dt = datetime.strptime(target_exp_input, "%m/%d/%q" if len(target_exp_input) == 8 else "%m/%d/%Y")
            target_exp_yf = dt.strftime("%Y-%m-%d")
        except ValueError:
            print_error_xml(f"Invalid date format: {target_exp_input}. Use MM/DD/YYYY.")

    # 2. Fetch Base Expirations
    ticker = yf.Ticker(ticker_symbol)
    expirations = ticker.options

    if not expirations:
        print_error_xml(f"No options chains found for ticker {ticker_symbol}.")

    # 3. Filter Expirations
    exps_to_scan = list(expirations)
    if target_exp_yf:
        if target_exp_yf not in expirations:
            print_error_xml(f"Expiration {target_exp_input} not available. Valid dates: {', '.join(expirations[:3])}...")
        exps_to_scan = [target_exp_yf]

    all_calls = []
    all_puts = []

    # 4. Pull the Data
    for exp in exps_to_scan:
        try:
            chain = ticker.option_chain(exp)
            
            calls = chain.calls
            calls['expiration'] = exp
            all_calls.append(calls)
            
            puts = chain.puts
            puts['expiration'] = exp
            all_puts.append(puts)
        except Exception:
            continue

    if not all_calls and not all_puts:
        print_error_xml("Failed to download chain data from Yahoo Finance.")

    # Combine DataFrames
    df_calls = pd.concat(all_calls, ignore_index=True)
    df_puts = pd.concat(all_puts, ignore_index=True)

    # Clean NaNs
    df_calls[['volume', 'openInterest']] = df_calls[['volume', 'openInterest']].fillna(0)
    df_puts[['volume', 'openInterest']] = df_puts[['volume', 'openInterest']].fillna(0)

    # 5. Build XML Document
    root = ET.Element("options_scan")
    root.set("ticker", ticker_symbol)
    root.set("scan_type", "SINGLE_EXP" if target_exp_yf else "ALL_EXPS")
    if target_exp_yf:
        root.set("target_date", target_exp_input)
    root.set("timestamp", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    # CALLS
    calls_elem = ET.SubElement(root, "calls")
    if not df_calls.empty:
        top_vol_call = df_calls.loc[df_calls['volume'].idxmax()]
        top_oi_call = df_calls.loc[df_calls['openInterest'].idxmax()]
        build_contract_xml(calls_elem, "highest_volume", top_vol_call)
        build_contract_xml(calls_elem, "highest_open_interest", top_oi_call)

    # PUTS
    puts_elem = ET.SubElement(root, "puts")
    if not df_puts.empty:
        top_vol_put = df_puts.loc[df_puts['volume'].idxmax()]
        top_oi_put = df_puts.loc[df_puts['openInterest'].idxmax()]
        build_contract_xml(puts_elem, "highest_volume", top_vol_put)
        build_contract_xml(puts_elem, "highest_open_interest", top_oi_put)

    # 6. Print Pretty XML
    xml_str = ET.tostring(root, encoding='utf-8')
    parsed = minidom.parseString(xml_str)
    print(parsed.toprettyxml(indent="  "))

if __name__ == "__main__":
    main()