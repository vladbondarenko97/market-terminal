import os
from config import required_env
import csv
import json
import base64
from core.api_client import api_client
import xml.etree.ElementTree as ET
from xml.dom import minidom
from datetime import datetime, timedelta
import time

# --- CONFIGURATION ---
from config import DATA_DIR, EBAY_APP_ID, EBAY_CERT_ID

# --- EBAY API CREDENTIALS ---
APP_ID = EBAY_APP_ID
CERT_ID = EBAY_CERT_ID

# --- 1. DYNAMIC SPOT PRICE INGESTION (TEXT PARSER) ---

def find_latest_files(lookback_days=5):
    today = datetime.now()
    found_dash = None
    found_tac = None

    for i in range(lookback_days + 1):
        check_date = today - timedelta(days=i)
        folder_str = check_date.strftime("%b-%d-%y")
        folder_path = os.path.join(DATA_DIR, folder_str)

        dash_path = os.path.join(folder_path, "volume_dashboard.txt")
        tac_path = os.path.join(folder_path, "tactical_ruling.txt")

        if not found_dash and os.path.exists(dash_path) and os.path.getsize(dash_path) > 0:
            found_dash = dash_path
        if not found_tac and os.path.exists(tac_path) and os.path.getsize(tac_path) > 0:
            found_tac = tac_path
        if found_dash and found_tac:
            break

    return found_dash, found_tac

def get_live_spot_price():
    """Benchmark = SI=F. Prefer the latest committed v2 snapshot (same run as the report); otherwise a live
    Yahoo quote. Returns None when unavailable: premiums are then reported as unavailable, never guessed."""
    try:
        from core import lake
        from config import DB_PATH
        conn = lake.connect(DB_PATH, readonly=True)
        ctx = lake.load_snapshot(conn)
        conn.close()
        v = (((ctx or {}).get("prices") or {}).get("silver_futures") or {}).get("value")
        if v:
            return float(v)
    except Exception:
        pass
    try:
        import yfinance as yf
        hist = yf.Ticker("SI=F").history(period="7d")
        if not hist.empty:
            return float(hist["Close"].dropna().iloc[-1])
    except Exception:
        pass
    return None


COMEX_SPOT = None

# --- 2. EBAY API ENGINE ---

def get_ebay_token():
    """Authenticates with eBay OAuth2 and returns an Application Access Token."""
    # Convert credentials to Base64 for the Basic Auth header
    auth_str = f"{APP_ID}:{CERT_ID}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()

    token_url = "https://api.ebay.com/identity/v1/oauth2/token"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {b64_auth}"
    }
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope"
    }

    try:
        resp = api_client.post(token_url, headers=headers, data=data)
        return resp.json().get("access_token")
    except Exception as e:
        print(f"Failed to get token: {e}")
        return None

def get_ebay_arbitrage_data_api(item_id, token):
    """Fetches an item using the official eBay Browse API and extracts inventory levels."""
    url = f"https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id?legacy_item_id={item_id}"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"
    }
    
    try:
        resp = api_client.get(url, headers=headers)
        data = resp.json()
        name = data.get("title", f"Silver Eagle (ID: {item_id})")
        
        price_data = data.get("price", {})
        base_price = float(price_data.get("value", 0.0))
        
        shipping_cost = 0.0
        shipping_options = data.get("shippingOptions", [])
        if shipping_options:
            ship_data = shipping_options[0].get("shippingCost", {})
            shipping_cost = float(ship_data.get("value", 0.0))
            
        status = "Unknown"
        available_qty = "0"
        sold_qty = "0"
        
        availabilities = data.get("estimatedAvailabilities", [])
        if availabilities:
            avail_node = availabilities[0]
            status = avail_node.get("estimatedAvailabilityStatus", "Unknown")
            
            if "availabilityThreshold" in avail_node:
                available_qty = f"{avail_node['availabilityThreshold']}+"
            else:
                available_qty = str(avail_node.get("estimatedAvailableQuantity", 0))
                
            sold_qty = str(avail_node.get("estimatedSoldQuantity", 0))
        
        return {
            "item_id": item_id,
            "name": name,
            "base_price": base_price,
            "shipping": shipping_cost,
            "total_cost": base_price + shipping_cost,
            "status": status,
            "available_qty": available_qty,
            "sold_qty": sold_qty
        }
    except Exception as e:
        if "404" in str(e):
            return {"error": f"Item {item_id} not found or listing removed."}
        return {"error": str(e)}

def log_arbitrage_ledger(cheapest_cost, avg_cost, comex_spot, item_count):
    """Appends the latest physical arbitrage metrics to a dedicated CSV ledger."""
    os.makedirs(DATA_DIR, exist_ok=True)
    
    file_path = os.path.join(DATA_DIR, "physical_arbitrage_ledger.csv")
    file_exists = os.path.isfile(file_path)

    headers = [
        "Datetime", "COMEX_Spot", "Cheapest_Eagle", "Average_Eagle", 
        "Cheapest_Premium_Dollars", "Cheapest_Premium_Percent", 
        "Average_Premium_Dollars", "Average_Premium_Percent", "Dealers_Scanned"
    ]

    cheapest_prem_dlr = cheapest_cost - comex_spot if cheapest_cost else 0
    cheapest_prem_pct = (cheapest_prem_dlr / comex_spot * 100) if comex_spot else 0
    avg_prem_dlr = avg_cost - comex_spot if avg_cost else 0
    avg_prem_pct = (avg_prem_dlr / comex_spot * 100) if comex_spot else 0

    with open(file_path, mode='a', newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(headers)
        
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M")
        
        row = [
            current_time, 
            round(comex_spot, 2), 
            round(cheapest_cost, 2) if cheapest_cost else "NaN", 
            round(avg_cost, 2) if avg_cost else "NaN",
            round(cheapest_prem_dlr, 2),
            round(cheapest_prem_pct, 2),
            round(avg_prem_dlr, 2),
            round(avg_prem_pct, 2),
            item_count
        ]
        writer.writerow(row)
        
        try:
            row_dict = dict(zip(headers, row))
            from core.sqlite_layer import append_row
            append_row('physical_arbitrage_ledger', row_dict)
        except Exception as e:
            print(f"⚠️ SQLite dual-write failed for ebay.py: {e}")

# --- 3. EXECUTION ENGINE ---
if __name__ == "__main__":
    # Remove playwright install dependencies since we rely purely on requests now
    target_items = [
        "116932983600", # apmex 2026 1oz silver eagle
        "145163361842", # apmex random 1oz silver eagle
        "389246127742", # mcm 2026 1oz silver eagle
        "336328938777", # aydin 2026 1oz silver eagle
        "205844257920", # pinehurst 2026 1oz silver eagle
        "317033453863", # pinehurst random 1oz silver eagle
        "303115011716", # liberty coin random 1oz silver eagle
        "135152154013", # mcm random 1oz silver eagle
        "406597959293"  # dbs 2026 1 oz silver eagle
    ] 
    
    scraped_data = []
    COMEX_SPOT = get_live_spot_price()

    # 1. Boot up API Engine
    token = get_ebay_token()
    if not token:
        print("Cannot proceed without a valid eBay API token.")
        exit(1)

    # 2. Query Items sequentially
    for item in target_items:
        intel = get_ebay_arbitrage_data_api(item, token)
        
        if "error" not in intel:
            if COMEX_SPOT:
                intel['premium_dollars'] = intel['total_cost'] - COMEX_SPOT
                intel['premium_percent'] = intel['premium_dollars'] / COMEX_SPOT * 100
            else:
                intel['premium_dollars'] = intel['premium_percent'] = None
            scraped_data.append(intel)
        else:
            # Silently log errors and continue (e.g., if a listing ends)
            pass
            
        # Optional: Add a very slight delay so we don't spam the API (allowed limit is 5k calls/day)
        time.sleep(0.1)

    # Sort by lowest premium percent mathematically
    scraped_data.sort(key=lambda x: x['total_cost'])

    # --- FIRE LEDGER ENTRY ---
    if len(scraped_data) > 0 and COMEX_SPOT:
        cheapest = scraped_data[0]['total_cost']
        avg = sum(d['total_cost'] for d in scraped_data) / len(scraped_data)
        log_arbitrage_ledger(cheapest, avg, COMEX_SPOT, len(scraped_data))

    # Build XML Root for the terminal
    root = ET.Element("physical_arbitrage", 
                      comex_spot=f"${COMEX_SPOT:.2f}" if COMEX_SPOT else "unavailable", 
                      timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                      item_count=str(len(scraped_data)))

    for d in scraped_data:
        listing = ET.SubElement(root, "listing")
        listing.set("item_id", d['item_id'])
        
        clean_name = d['name']
        listing.set("name", clean_name[:60] + ("..." if len(clean_name) > 60 else "")) 
        
        listing.set("total_cost", f"${d['total_cost']:.2f}")
        listing.set("base_price", f"${d['base_price']:.2f}")
        listing.set("shipping", f"${d['shipping']:.2f}")
        listing.set("premium_dollars", f"${d['premium_dollars']:.2f}" if d['premium_dollars'] is not None else "unavailable")
        listing.set("premium_percent", f"{d['premium_percent']:.2f}%" if d['premium_percent'] is not None else "unavailable")
        
        # --- NEW INVENTORY ATTRIBUTES ---
        listing.set("status", d['status'])
        listing.set("available_qty", d['available_qty'])
        listing.set("sold_qty", d['sold_qty'])

    # Output Pretty XML back into the terminal's data pipeline
    raw_xml = ET.tostring(root, encoding='utf-8')
    pretty_xml = minidom.parseString(raw_xml).toprettyxml(indent="  ")
    
    print(pretty_xml)