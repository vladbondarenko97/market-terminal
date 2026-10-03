from flask import Flask, request, Response
import yfinance as yf
import pandas as pd
from datetime import datetime
import xml.etree.ElementTree as ET
from xml.dom import minidom
import warnings

warnings.simplefilter(action='ignore', category=FutureWarning)

app = Flask(__name__)

def build_error_xml(message):
    root = ET.Element("error")
    root.text = message
    xml_str = minidom.parseString(ET.tostring(root)).toprettyxml(indent="  ")
    return Response(xml_str, mimetype='application/xml', status=400)

def build_contract_xml(parent, tag_name, contract):
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

@app.route('/api/find_options', methods=['GET'])
def find_options():
    # Require Ticker
    ticker_symbol = request.args.get('ticker')
    if not ticker_symbol:
        return build_error_xml("Missing required parameter: ticker (e.g., ?ticker=SPY)")
    ticker_symbol = ticker_symbol.upper()

    # Accept either 'exp' or 'expiration'
    target_exp_input = request.args.get('exp') or request.args.get('expiration')

    # Parse Date
    target_exp_yf = None
    if target_exp_input:
        try:
            dt = datetime.strptime(target_exp_input, "%m/%d/%y" if len(target_exp_input) == 8 else "%m/%d/%Y")
            target_exp_yf = dt.strftime("%Y-%m-%d")
        except ValueError:
            return build_error_xml(f"Invalid date format: {target_exp_input}. Use MM/DD/YYYY.")

    ticker = yf.Ticker(ticker_symbol)
    expirations = ticker.options

    if not expirations:
        return build_error_xml(f"No options chains found for ticker {ticker_symbol}.")

    exps_to_scan = list(expirations)
    if target_exp_yf:
        if target_exp_yf not in expirations:
            return build_error_xml(f"Expiration {target_exp_input} not available.")
        exps_to_scan = [target_exp_yf]

    all_calls, all_puts = [], []

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
        return build_error_xml("Failed to download chain data from Yahoo Finance.")

    df_calls = pd.concat(all_calls, ignore_index=True)
    df_puts = pd.concat(all_puts, ignore_index=True)
    df_calls[['volume', 'openInterest']] = df_calls[['volume', 'openInterest']].fillna(0)
    df_puts[['volume', 'openInterest']] = df_puts[['volume', 'openInterest']].fillna(0)

    # Build XML
    root = ET.Element("options_scan")
    root.set("ticker", ticker_symbol)
    root.set("scan_type", "SINGLE_EXP" if target_exp_yf else "ALL_EXPS")
    if target_exp_yf:
        root.set("target_date", target_exp_input)
    root.set("timestamp", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    calls_elem = ET.SubElement(root, "calls")
    if not df_calls.empty:
        build_contract_xml(calls_elem, "highest_volume", df_calls.loc[df_calls['volume'].idxmax()])
        build_contract_xml(calls_elem, "highest_open_interest", df_calls.loc[df_calls['openInterest'].idxmax()])

    puts_elem = ET.SubElement(root, "puts")
    if not df_puts.empty:
        build_contract_xml(puts_elem, "highest_volume", df_puts.loc[df_puts['volume'].idxmax()])
        build_contract_xml(puts_elem, "highest_open_interest", df_puts.loc[df_puts['openInterest'].idxmax()])

    xml_str = minidom.parseString(ET.tostring(root, encoding='utf-8')).toprettyxml(indent="  ")
    
    # Return as actual XML so the browser renders it beautifully
    return Response(xml_str, mimetype='application/xml')

if __name__ == "__main__":
    # Listen on port 5002 
    app.run(host='0.0.0.0', port=8080)