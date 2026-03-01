# config.py — Trading bot configuration

ALPACA_KEY = "PKTXUJOPHVMKKKUE36AZQ57ADU"
ALPACA_SECRET = "JBXDtEWZS3eKa8yzXzFzd4SgUdmqBC6ixKzNFyAvHd3"
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"

TELEGRAM_GROUP = "-5191423233"
OPENCLAW_GATEWAY = "http://127.0.0.1:18789/api/messages/send"
SEARXNG_URL = "http://127.0.0.1:8888/search"

WATCHLIST = ["SPY", "QQQ", "AAPL", "TSLA", "NVDA", "AMD", "MSFT", "META", "GME", "AMZN"]

STARTING_CAPITAL = 500.0
MAX_POSITIONS = 5
MIN_POSITIONS = 3
POSITION_SIZE_MIN = 0.10   # 10% of cash
POSITION_SIZE_MAX = 0.25   # 25% of cash
STOP_LOSS_PCT = -0.08       # -8%
TAKE_PROFIT_PCT = 0.15      # +15%
MIN_SIGNAL_SCORE = 5        # Only trade if score >= 5

TRADES_DIR = "/home/pavano/.openclaw/workspace/trading/trades"
SIGNALS_FILE = "/home/pavano/.openclaw/workspace/trading/signals_output.json"

# Options flow thresholds
UNUSUAL_VOLUME_MULTIPLIER = 2.0   # >2x avg OI
MIN_EXPIRY_DAYS = 7
MAX_EXPIRY_DAYS = 30
