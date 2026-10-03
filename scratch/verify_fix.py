
import sys
import os

# Add project root to path
sys.path.append(__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__))))

from core.api_client import api_client
import time

def test_api_client_hang():
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    print(f"Testing api_client with URL: {url}")
    start_time = time.time()
    try:
        # We expect this to fail fast because of the 429 and disabled retry-after
        resp = api_client.get(url, headers=headers, timeout=5)
        print(f"Status Code: {resp.status_code}")
    except Exception as e:
        print(f"Caught expected exception or failure: {e}")
    
    end_time = time.time()
    duration = end_time - start_time
    print(f"Duration: {duration:.2f} seconds")
    
    if duration < 30:
        print("SUCCESS: API client did not hang for minutes.")
    else:
        print("FAILURE: API client still hangs.")

if __name__ == "__main__":
    test_api_client_hang()
