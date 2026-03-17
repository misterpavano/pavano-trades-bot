# config.py — Trading bot configuration (OPTIONS ONLY)
import os
import json

def _load_secret(env_var: str, fallback_file: str = None) -> str:
    """Load a secret from environment variable or fallback file (~/.secrets)."""
    val = os.environ.get(env_var)
    if val:
        return val
    # Try loading from ~/.secrets if not in env
    secrets_file = os.path.expanduser("~/.secrets")
    if os.path.exists(secrets_file):
        with open(secrets_file) as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{env_var}="):
                    # Handle quoted values
                    val = line.split("=", 1)[1].strip('"').strip("'")
                    return val
    raise RuntimeError(f"{env_var} not set — add to environment or ~/.secrets")

def _get_telegram_token() -> str:
    """Load Telegram bot token from openclaw.json."""
    cfg_path = "/home/pavano/.openclaw/openclaw.json"
    try:
        with open(cfg_path) as f:
            return json.load(f)["channels"]["telegram"]["botToken"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Cannot load Telegram token from openclaw.json: {e}")

ALPACA_KEY = _load_secret("ALPACA_KEY")
ALPACA_SECRET = _load_secret("ALPACA_SECRET")
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"

TELEGRAM_BOT_TOKEN = _get_telegram_token()
TELEGRAM_GROUP = "-5191423233"
OPENCLAW_GATEWAY = "http://127.0.0.1:18789/hooks/agent"
SEARXNG_URL = "http://127.0.0.1:8888/search"

WATCHLIST = [
    # Core large caps
    "SPY", "QQQ", "AAPL", "TSLA", "NVDA", "AMD", "MSFT", "META", "GME", "AMZN",
    # Airlines (sector momentum plays)
    "DAL", "AAL", "UAL", "LUV",
    # High-options-volume momentum names
    "PLTR", "SOFI", "SOUN", "MSTR", "COIN", "HOOD", "RKLB",
    # Sector ETFs (catch macro flow)
    "XLF", "XLE", "XLK", "ARKK",
]

STARTING_CAPITAL = 1000.0
MAX_POSITIONS = 5           # max 5 open option positions at once
MIN_SIGNAL_SCORE = 3        # Lowered from 5 — threshold was too high, nothing ever fired
OPEN_ENTRY_DELAY_MINUTES = 15  # Wait N minutes after 9:30am before entering — avoids opening noise

# Options-specific config
STOP_LOSS_PCT = -0.50       # -50% on option premium
TAKE_PROFIT_PCT = 1.00      # +100% on option premium (double up)
MAX_POSITION_COST = 190     # max per options position — keeps us under $207 balance
CASH_RESERVE = 50           # minimum cash buffer — never go to $0
MAX_CONTRACT_ASK = 1.90     # skip if ask > $1.90/share ($190/contract) — sized for current balance
OPTION_DTE_MIN = 7          # min days to expiry
OPTION_DTE_MAX = 30         # max days to expiry
OTM_PCT = 0.02              # target 2% OTM strikes

TRADES_DIR = "/home/pavano/pavano-trades-bot/trades"
SIGNALS_FILE = "/home/pavano/pavano-trades-bot/signals_output.json"
SIGNALS_EOD_FILE = "/home/pavano/pavano-trades-bot/signals_eod.json"

# Options flow thresholds
UNUSUAL_VOLUME_MULTIPLIER = 3.0  # lowered from 5.0 — was filtering too aggressively, nothing fired
MIN_EXPIRY_DAYS = 7
MAX_EXPIRY_DAYS = 30
