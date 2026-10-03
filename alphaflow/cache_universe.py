import os
import pandas as pd
import yfinance as yf
import time

CACHE_FILE = os.path.join(os.path.dirname(__file__), "small_caps.csv")

def build_universe_cache():
    # Hardcoded known small-caps for safety/demonstration, instead of scraping 3000
    # In a full run, we'd read a master list of all US equities
    master_list = ["SPXS", "SEDG", "BZH", "RIG", "SOUN", "PLUG", "RIOT", "MARA", "CVNA", "MSTR", "NKLA", "RUN", "FSLR", "ENPH", "GME", "AMC"]
    
    results = []
    print(f"Checking market caps for {len(master_list)} tickers...")
    
    for ticker in master_list:
        try:
            tkr = yf.Ticker(ticker)
            info = tkr.info
            mcap = info.get("marketCap", 0)
            
            # Filter $100M - $10B
            if 100_000_000 <= mcap <= 10_000_000_000:
                print(f"[{ticker}] MCap: ${mcap/1e9:.2f}B - Passes criteria.")
                # We won't iterate all OI here to save time for testing, 
                # but we will get the nearest expiration's total OI as an approximation
                total_oi = 1000  # Default fallback
                try:
                    expirations = tkr.options
                    if expirations:
                        nearest = expirations[0]
                        opt_chain = tkr.option_chain(nearest)
                        total_oi = opt_chain.calls['openInterest'].sum()
                except Exception:
                    pass
                
                results.append({
                    "Ticker": ticker,
                    "MarketCap": mcap,
                    "Total_OI_Approx": max(int(total_oi), 1) # Ensure non-zero
                })
        except Exception as e:
            print(f"Error fetching {ticker}: {e}")
            
        time.sleep(0.5) # Prevent rate limiting
        
        # Hard cap for databento testing protection
        if len(results) >= 50:
            print("Hit 50 ticker cap for Databento protection.")
            break
            
    df = pd.DataFrame(results)
    df.to_csv(CACHE_FILE, index=False)
    print(f"Saved {len(df)} tickers to small_caps.csv")

if __name__ == '__main__':
    build_universe_cache()
