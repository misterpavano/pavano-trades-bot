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
MAX_POSITIONS = 4           # max 4 open option positions — concentrated bets, not spray & pray
MIN_SIGNAL_SCORE = 4        # Raised from 3 — fewer trades, higher conviction only
OPEN_ENTRY_DELAY_MINUTES = 15  # Wait N minutes after 9:30am before entering — avoids opening noise

# Options-specific config
STOP_LOSS_PCT = -0.35       # -35% on option premium (was -50% — too much rope, they hang themselves)
TAKE_PROFIT_PCT = 0.75      # +75% on option premium (was 100% — take profit before it evaporates)
TRAILING_STOP_ACTIVATE = 0.40  # activate trailing stop at +40% gain
TRAILING_STOP_PCT = 0.20    # trail 20% from high — locks in at least +20% if we hit +40%
MAX_POSITION_COST = 200     # max per options position — 20% of $1000 account
CASH_RESERVE = 200          # minimum cash buffer — always keep dry powder for opportunities
MAX_CONTRACT_ASK = 2.00     # skip if ask > $2.00/share ($200/contract)
OPTION_DTE_MIN = 14         # min days to expiry (was 7 — too short, theta ate us alive)
OPTION_DTE_MAX = 45         # max days to expiry (was 30 — more room to be right)
OTM_PCT = 0.03              # target 3% OTM strikes (was 2% — slightly more room)

TRADES_DIR = "/home/pavano/pavano-trades-bot/trades"
SIGNALS_FILE = "/home/pavano/pavano-trades-bot/signals_output.json"
SIGNALS_EOD_FILE = "/home/pavano/pavano-trades-bot/signals_eod.json"

# Options flow thresholds
UNUSUAL_VOLUME_MULTIPLIER = 3.0  # lowered from 5.0 — was filtering too aggressively, nothing fired
MIN_EXPIRY_DAYS = 7
MAX_EXPIRY_DAYS = 30
