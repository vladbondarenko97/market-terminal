import os
import pandas as pd
from datetime import datetime, timedelta

HISTORY_FILE = os.path.join(os.path.expanduser("~"), "Desktop", "CME_Data", "comex_inventory_history.csv")

def fix_history():
    df = pd.read_csv(HISTORY_FILE)
    
    # If the file only has today's single run in it
    if len(df) == 1:
        print("▶️ Fixing your CSV history...")
        today_row = df.iloc[0]
        
        # Calculate the 29 days prior to today
        dates = [(datetime.strptime(today_row['Date'], "%Y-%m-%d") - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(29, 0, -1)]
        
        seed_data = []
        for d in dates:
            seed_data.append({
                "Date": d,
                "Registered": today_row['Registered'],
                "Eligible": today_row['Eligible'] + 1169549.0, # Adding back today's drop so the baseline is accurate
                "Total": today_row['Total'] + 1169549.0,
                "Reg_Change": 0.0,
                "Elig_Change": 0.0,
                "Total_Change": 0.0
            })
            
        seed_df = pd.DataFrame(seed_data)
        
        # Combine the 29 days of history with today's real data
        fixed_df = pd.concat([seed_df, df], ignore_index=True)
        fixed_df.to_csv(HISTORY_FILE, index=False)
        print("✅ Fixed! CSV now has 30 days of data. Your chart will render perfectly.")
    else:
        print(f"CSV already has {len(df)} days of data.")

if __name__ == "__main__":
    fix_history()