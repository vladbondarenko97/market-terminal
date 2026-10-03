import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Base Paths
PROJECT_ROOT = Path(__file__).resolve().parent
DOTENV_PATH = PROJECT_ROOT / ".env"

try:
    from dotenv import load_dotenv
    load_dotenv(DOTENV_PATH)
except ImportError:
    pass


class ConfigError(RuntimeError):
    pass


def _resolve_data_dir():
    """Resolve CME_Data once.

    Order: PORTFOLIO_DATA_DIR (explicit) -> the project's sibling CME_Data (historical default).
    If the sibling and ~/Desktop/CME_Data are different directories that both hold a portfolio.db,
    refuse to guess: the operator must set PORTFOLIO_DATA_DIR.
    """
    explicit = os.environ.get("PORTFOLIO_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    sibling = (PROJECT_ROOT.parent / "CME_Data").resolve()
    desktop = (Path.home() / "Desktop" / "CME_Data").resolve()
    if sibling != desktop and (sibling / "portfolio.db").exists() and (desktop / "portfolio.db").exists():
        raise ConfigError(
            f"Two CME_Data installations found ({sibling} and {desktop}). "
            "Set PORTFOLIO_DATA_DIR to the intended one."
        )
    if not sibling.exists() and (desktop / "portfolio.db").exists():
        return desktop
    return sibling


DATA_DIR = _resolve_data_dir()
DB_PATH = DATA_DIR / "portfolio.db"

# All run-folder and report-date decisions use Chicago time (CME's home zone).
TZ_NAME = "America/Chicago"
LOCAL_TZ = ZoneInfo(TZ_NAME)
RUN_FOLDER_FORMAT = "%b-%d-%y"


def now_utc():
    return datetime.now(timezone.utc)


def run_folder_name(moment=None):
    """Daily folder name (e.g. Sep-29-26), computed in America/Chicago."""
    moment = moment or now_utc()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(LOCAL_TZ).strftime(RUN_FOLDER_FORMAT)


def daily_dir(moment=None):
    return DATA_DIR / run_folder_name(moment)


def ensure_data_dir(allow_create=False):
    """Never silently create a fresh, empty installation somewhere new."""
    if DATA_DIR.exists():
        return DATA_DIR
    if not allow_create:
        raise ConfigError(f"Data directory {DATA_DIR} does not exist. Set PORTFOLIO_DATA_DIR or create it explicitly.")
    DATA_DIR.mkdir(parents=True)
    return DATA_DIR


def required_env(name, alt_name=None):
    value = os.environ.get(name)
    if (value is None or value == "") and alt_name:
        value = os.environ.get(alt_name)
    if value is None or value == "":
        if alt_name:
            raise EnvironmentError(
                f"Missing required environment variable '{name}' (or fallback '{alt_name}')"
            )
        raise EnvironmentError(f"Missing required environment variable '{name}'")
    return value


def optional_env(name, default=None, alt_name=None):
    value = os.environ.get(name)
    if value is None and alt_name:
        value = os.environ.get(alt_name)
    return value if value is not None else default


# Legacy scripts create the directory themselves; keep that behaviour only when it already exists
# or when running from the historical sibling layout.
if not DATA_DIR.exists() and DATA_DIR == (PROJECT_ROOT.parent / "CME_Data").resolve():
    os.makedirs(DATA_DIR, exist_ok=True)

# Centralized API Keys & Config. Credentials are validated lazily: importing config never requires them.
DATABENTO_API_KEY = optional_env("DATABENTO_API_KEY", default="", alt_name="DB_API_KEY")
FRED_API_KEY = optional_env("FRED_API_KEY", default="")
EIA_API_KEY = optional_env("EIA_API_KEY", default="")
ALPHA_VANTAGE_KEY = optional_env("ALPHA_VANTAGE_KEY", default="")
GOLD_API_KEY = optional_env("GOLD_API_KEY", default="")
EBAY_APP_ID = optional_env("EBAY_APP_ID", default="")
EBAY_CERT_ID = optional_env("EBAY_CERT_ID", default="")
EMAIL_SENDER = optional_env("EMAIL_SENDER", default="")
EMAIL_PASSWORD = optional_env("EMAIL_PASSWORD", default="")
CME_LOGIN_USERNAME = optional_env("CME_LOGIN_USERNAME", default="")
CME_LOGIN_PASSWORD = optional_env("CME_LOGIN_PASSWORD", default="")

# Delivery identity: all from .env; an empty value switches that channel off
SMTP_SERVER = optional_env("SMTP_SERVER", default="")
SMTP_PORT = int(optional_env("SMTP_PORT", default="587"))
RECIPIENT_EMAIL = optional_env("RECIPIENT_EMAIL", default="")
NTFY_URL = optional_env("NTFY_URL", default="")
UPLOAD_URL = optional_env("UPLOAD_URL", default="")
REPORT_SENDER = optional_env("REPORT_SENDER", default="") or EMAIL_SENDER

# Source limits
MAX_PROVIDER_CONCURRENCY = 3
CME_MAX_ATTEMPTS_PER_URL = 2
CME_429_COOLDOWN_SECONDS = 20
CME_BACKFILL_MAX_ATTEMPTS = 40


def require_databento_key():
    return required_env("DATABENTO_API_KEY", alt_name="DB_API_KEY")
