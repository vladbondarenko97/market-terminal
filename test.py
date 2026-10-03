import yfinance as yf
import json

# 1. Use the Ticker object to bypass the MultiIndex column error
spy = yf.Ticker("SPY")

# 2. Fetch minute-by-minute data for the last 5 days
spy_data = spy.history(period="5d", interval="1m")

# 3. Format the data into a clean JSON array
wick_data = []
for index, row in spy_data.iterrows():
    # Filter out empty/NaN rows that sometimes occur outside market hours
    if not row.isna().any():
        wick = {
            "timestamp": str(index),
            "open": round(row['Open'], 2),
            "high": round(row['High'], 2),
            "low": round(row['Low'], 2),
            "close": round(row['Close'], 2),
            "volume": int(row['Volume'])
        }
        wick_data.append(wick)

# 4. Export directly to a JSON file for the LLM
with open("spy_wicks_1m.json", "w") as f:
    json.dump(wick_data, f, indent=4)

print(f"SYSTEM NOTIFICATION: Successfully extracted {len(wick_data)} minute-by-minute wicks to spy_wicks_1m.json")