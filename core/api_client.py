import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class UnifiedAPIClient:
    def __init__(self, timeout=15, retries=3, backoff_factor=0.3):
        self.timeout = timeout
        self.session = requests.Session()
        
        # Standard Headers
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'
        })
        
        # Retry logic
        retry = Retry(
            total=retries,
            read=retries,
            connect=retries,
            backoff_factor=backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS", "POST"],
            respect_retry_after_header=False
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        
    def get(self, url, **kwargs):
        kwargs.setdefault('timeout', self.timeout)
        try:
            response = self.session.get(url, **kwargs)
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            logger.warning(f"GET Request failed for {url}: {e}")
            raise
            
    def post(self, url, **kwargs):
        kwargs.setdefault('timeout', self.timeout)
        try:
            response = self.session.post(url, **kwargs)
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            logger.warning(f"POST Request failed for {url}: {e}")
            raise

# Provide a default singleton instance to use across the project
api_client = UnifiedAPIClient()
