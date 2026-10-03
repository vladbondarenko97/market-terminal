
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

def test_macro_url():
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
    headers = {'User-Agent': 'Mozilla/5.0'}
    print(f"Testing URL: {url}")
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        print(f"Status Code: {resp.status_code}")
        print(f"Content Length: {len(resp.content)}")
        root = ET.fromstring(resp.content)
        events = root.findall('event')
        print(f"Found {len(events)} events")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_macro_url()
