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


def _blank(value):
    return value is None or not str(value).strip()


def _resolve_data_dir():
    """Resolve CME_Data once. Pure: nothing is created.

    Order: PORTFOLIO_DATA_DIR (explicit) -> the project's sibling CME_Data (historical default).
    If the sibling and ~/Desktop/CME_Data are different directories that both hold a portfolio.db,
    refuse to guess: the operator must set PORTFOLIO_DATA_DIR.
    """
    explicit = os.environ.get("PORTFOLIO_DATA_DIR", "").strip()
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


# True when the operator pinned the folder with PORTFOLIO_DATA_DIR. An explicit folder is never created for them.
DATA_DIR_EXPLICIT = bool(os.environ.get("PORTFOLIO_DATA_DIR", "").strip())
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
    """Return DATA_DIR, which must exist. Importing config never creates it.

    allow_create=True creates the default sibling CME_Data only. A folder named by PORTFOLIO_DATA_DIR is never
    created here, so a wrong path copied from another Mac is reported instead of becoming a fresh, empty
    installation somewhere new."""
    if DATA_DIR.exists():
        return DATA_DIR
    if allow_create and not DATA_DIR_EXPLICIT:
        DATA_DIR.mkdir(parents=True)
        return DATA_DIR
    hint = ("PORTFOLIO_DATA_DIR is set, so the folder is never created automatically: create it or fix the path."
            if DATA_DIR_EXPLICIT else "Set PORTFOLIO_DATA_DIR or create it explicitly.")
    raise ConfigError(f"Data directory {DATA_DIR} does not exist. {hint}")


def _first_env(name, alt_name=None):
    """The value of `name`; the alias `alt_name` when `name` is unset or blank; None when neither has a value."""
    for key in (name, alt_name):
        if key and not _blank(os.environ.get(key)):
            return os.environ[key]
    return None


def optional_env(name, default=None, alt_name=None):
    """The setting, or `default` when it is unset or blank (`NAME=` in .env counts as not configured)."""
    value = _first_env(name, alt_name)
    return value if value is not None else default


def int_env(name, default, *, minimum=None, maximum=None):
    """A whole-number setting. Unset or blank gives `default`; text that is not a number is a ConfigError."""
    raw = optional_env(name, default=None)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise ConfigError(f"{name} must be a whole number (got {raw!r}). Fix it in .env or leave it blank "
                          f"for the default, {default}.") from None
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise ConfigError(f"{name} must be between {minimum} and {maximum} (got {value}).")
    return value


def flag_env(name):
    """A switch: True only for exactly "1"."""
    return os.environ.get(name, "").strip() == "1"


# Centralized API Keys & Config. Credentials are validated lazily: importing config never requires them.
# DB_API_KEY is the legacy name of DATABENTO_API_KEY; it is used whenever the primary is unset or blank.
DATABENTO_API_KEY = optional_env("DATABENTO_API_KEY", default="", alt_name="DB_API_KEY")
FRED_API_KEY = optional_env("FRED_API_KEY", default="")
EIA_API_KEY = optional_env("EIA_API_KEY", default="")
GOLD_API_KEY = optional_env("GOLD_API_KEY", default="")
EBAY_APP_ID = optional_env("EBAY_APP_ID", default="")
EBAY_CERT_ID = optional_env("EBAY_CERT_ID", default="")
EMAIL_SENDER = optional_env("EMAIL_SENDER", default="")
EMAIL_PASSWORD = optional_env("EMAIL_PASSWORD", default="")
CME_LOGIN_USERNAME = optional_env("CME_LOGIN_USERNAME", default="")
CME_LOGIN_PASSWORD = optional_env("CME_LOGIN_PASSWORD", default="")

# Delivery identity: all from .env; an empty value switches that channel off
SMTP_SERVER = optional_env("SMTP_SERVER", default="")
SMTP_PORT = int_env("SMTP_PORT", 587, minimum=1, maximum=65535)
RECIPIENT_EMAIL = optional_env("RECIPIENT_EMAIL", default="")
NTFY_URL = optional_env("NTFY_URL", default="")
DASHBOARD_URL = optional_env("DASHBOARD_URL", default="")
UPLOAD_URL = optional_env("UPLOAD_URL", default="")
UPLOAD_TOKEN = optional_env("UPLOAD_TOKEN", default="")
REPORT_UPLOAD = flag_env("REPORT_UPLOAD")
REPORT_SENDER = optional_env("REPORT_SENDER", default="") or EMAIL_SENDER
# Only the machine that owns the schedule (setup.sh --schedule sets this) records scheduled runs: one source of truth.
SCHEDULED_RUNS = flag_env("SCHEDULED_RUNS")

# Per-machine and standalone-tool settings
OPTIONS_WHALE_PORT = int_env("OPTIONS_WHALE_PORT", 8080, minimum=1, maximum=65535)
ALPHAFLOW_DEBUG = flag_env("ALPHAFLOW_DEBUG")

# Source limits
MAX_PROVIDER_CONCURRENCY = 3
CME_MAX_ATTEMPTS_PER_URL = 2
CME_429_COOLDOWN_SECONDS = 20
CME_BACKFILL_MAX_ATTEMPTS = 40
