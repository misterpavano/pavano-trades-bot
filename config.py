# config.py — Trading bot configuration (OPTIONS ONLY)

ALPACA_KEY = "PKIEH4JNZ4PGVJWUVKXMXN3T3U"
ALPACA_SECRET = "EZbJLoMZhqdzPTukNv5u86WyKw6AgfL6BswKpT3Fkqx5"
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"

TELEGRAM_GROUP = "-5191423233"
OPENCLAW_GATEWAY = "http://127.0.0.1:18789/hooks/agent"
SEARXNG_URL = "http://127.0.0.1:8888/search"

WATCHLIST = ["SPY", "QQQ", "AAPL", "TSLA", "NVDA", "AMD", "MSFT", "META", "GME", "AMZN"]

STARTING_CAPITAL = 1000.0
MAX_POSITIONS = 5           # max 3 open option positions at once
MIN_SIGNAL_SCORE = 5        # Only trade if score >= 5

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
UNUSUAL_VOLUME_MULTIPLIER = 5.0  # raised from 2.0 — industry standard for true unusual activity
MIN_EXPIRY_DAYS = 7
MAX_EXPIRY_DAYS = 30
