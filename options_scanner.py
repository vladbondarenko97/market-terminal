import yfinance as yf
import pandas as pd
from datetime import datetime

def get_top_options(ticker_symbol):
    print(f"Fetching expiration dates for {ticker_symbol}...")
    ticker = yf.Ticker(ticker_symbol)
    expirations = ticker.options

    if not expirations:
        print(f"No options data found for {ticker_symbol}.")
        return None

    all_calls = []
    all_puts = []

    print(f"Scanning {len(expirations)} expiration chains for {ticker_symbol}. This takes a few seconds...")

    # Loop through every available expiration date
    for exp in expirations:
        try:
            chain = ticker.option_chain(exp)
            
            # Grab calls, tag with expiration date, and append
            calls = chain.calls
            calls['expiration'] = exp
            all_calls.append(calls)
            
            # Grab puts, tag with expiration date, and append
            puts = chain.puts
            puts['expiration'] = exp
            all_puts.append(puts)
        except Exception as e:
            # Skip if a specific date chain is broken on Yahoo's end
            continue

    if not all_calls or not all_puts:
        return None

    # Combine all the individual date dataframes into one master chain
    df_calls = pd.concat(all_calls, ignore_index=True)
    df_puts = pd.concat(all_puts, ignore_index=True)

    # Unknown volume/OI stays unknown: rank only contracts where the provider supplied the value.
    def top(df, col):
        vals = pd.to_numeric(df[col], errors='coerce')
        return df.loc[vals.idxmax()] if vals.notna().any() else None

    return {
        "top_vol_call": top(df_calls, 'volume'),
        "top_oi_call": top(df_calls, 'openInterest'),
        "top_vol_put": top(df_puts, 'volume'),
        "top_oi_put": top(df_puts, 'openInterest')
    }

def format_contract(contract, title):
    if contract is None:
        return f"  [{title}] unavailable (provider supplied no values)"
    def num(v, fmt):
        return fmt.format(v) if pd.notna(v) else "unknown"
    return f"""  [{title}] Strike: ${contract['strike']} | Exp: {contract['expiration']}
    - Volume: {num(contract['volume'], '{:,.0f}')} | Open Interest: {num(contract['openInterest'], '{:,.0f}')}
    - Last Price: {num(contract['lastPrice'], '${:.2f}')} | Implied Volatility: {num(contract['impliedVolatility'] * 100 if pd.notna(contract['impliedVolatility']) else float('nan'), '{:.2f}%')}
    - Symbol: {contract['contractSymbol']}"""

def generate_options_report():
    output = []
    output.append(f"\n--- OPTIONS WHALE SCANNER ---")
    output.append(f"Time: {datetime.now().strftime('%Y-%m-%d %I:%M %p')}\n")

    # 1. Scan SPY
    spy_data = get_top_options("SPY")
    if spy_data:
        output.append("\n" + "="*40)
        output.append(" 🦅 SPY (S&P 500 ETF) OPTIONS")
        output.append("="*40)
        output.append("🔥 CALLS:")
        output.append(format_contract(spy_data['top_vol_call'], "Highest Volume Call"))
        output.append(format_contract(spy_data['top_oi_call'], "Highest OI Call"))
        output.append("\n🩸 PUTS:")
        output.append(format_contract(spy_data['top_vol_put'], "Highest Volume Put"))
        output.append(format_contract(spy_data['top_oi_put'], "Highest OI Put"))

    output.append("\n")

    # 2. Scan SLV
    slv_data = get_top_options("SLV")
    if slv_data:
        output.append("\n" + "="*40)
        output.append(" 🪙 SLV (SILVER TRUST) OPTIONS")
        output.append("="*40)
        output.append("🔥 CALLS:")
        output.append(format_contract(slv_data['top_vol_call'], "Highest Volume Call"))
        output.append(format_contract(slv_data['top_oi_call'], "Highest OI Call"))
        output.append("\n🩸 PUTS:")
        output.append(format_contract(slv_data['top_vol_put'], "Highest Volume Put"))
        output.append(format_contract(slv_data['top_oi_put'], "Highest OI Put"))

    output.append("\n--- SCAN COMPLETE ---")
    return "\n".join(output)

if __name__ == "__main__":
    # Suppress pandas FutureWarnings that occasionally pop up with yfinance concatenations
    import warnings
    warnings.simplefilter(action='ignore', category=FutureWarning)
    
    print(generate_options_report())