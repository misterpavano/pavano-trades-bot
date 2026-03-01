# config.py — Trading bot configuration (OPTIONS ONLY)

ALPACA_KEY = "PKTXUJOPHVMKKKUE36AZQ57ADU"
ALPACA_SECRET = "JBXDtEWZS3eKa8yzXzFzd4SgUdmqBC6ixKzNFyAvHd3"
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"

TELEGRAM_GROUP = "-5191423233"
OPENCLAW_GATEWAY = "http://127.0.0.1:18789/api/messages/send"
SEARXNG_URL = "http://127.0.0.1:8888/search"

WATCHLIST = ["SPY", "QQQ", "AAPL", "TSLA", "NVDA", "AMD", "MSFT", "META", "GME", "AMZN"]

STARTING_CAPITAL = 500.0
MAX_POSITIONS = 3           # max 3 open option positions at once
MIN_SIGNAL_SCORE = 5        # Only trade if score >= 5

# Options-specific config
STOP_LOSS_PCT = -0.50       # -50% on option premium
TAKE_PROFIT_PCT = 1.00      # +100% on option premium (double up)
MAX_POSITION_COST = 100     # max $100 per options position (1-2 contracts typically)
CASH_RESERVE = 200          # always keep $200 in reserve
MAX_CONTRACT_ASK = 2.00     # skip if ask > $2.00/share ($200/contract)
OPTION_DTE_MIN = 7          # min days to expiry
OPTION_DTE_MAX = 30         # max days to expiry
OTM_PCT = 0.03              # target 3% OTM strikes

TRADES_DIR = "/home/pavano/pavano-trades-bot/trades"
SIGNALS_FILE = "/home/pavano/pavano-trades-bot/signals_output.json"

# Options flow thresholds
UNUSUAL_VOLUME_MULTIPLIER = 2.0
MIN_EXPIRY_DAYS = 7
MAX_EXPIRY_DAYS = 30
