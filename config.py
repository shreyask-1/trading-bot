"""
API keys go here. This file is kept separate from the logic so you only
ever have to touch ONE place when setting up or rotating keys.

THIS VERSION reads keys from environment variables (used by GitHub Actions,
which injects your repo's encrypted "Secrets" as environment variables at
run time). This means your real key values are never typed into this file
or stored in your GitHub repo -- only referenced by name.

If you're running this locally instead (not via GitHub Actions), you can
just hardcode your real keys directly below each os.environ.get(...) call's
second argument, the same way the original local config.py worked.
"""

import os

FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "d9gglj1r01qq65366af0d9gglj1r01qq65366afg")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6LuDmKcUOMpMzcymMUdmf7QxuiRXH35hezCxeQ-_0kMkw")

# Alpaca PAPER TRADING keys (from the "Paper Trading" view of your dashboard,
# NOT the live trading keys). Get these at https://app.alpaca.markets
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "PPKBWCDYWNRY5IPPRCURYBFLEM2")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "FZX3xsCD2pArVJKZwMMCLgKDEQ6VguBCbUbp1RXJTaxX")

# --- Bot behavior settings ---
STARTING_CASH = 100_000.00       # fake starting balance
MAX_POSITION_PCT = 0.10          # never put more than 10% of portfolio in one stock (diversification)
MAX_NEWS_ITEMS = 15              # how many news headlines to send to Gemini per run
GEMINI_MODEL = "gemini-flash-latest"  # Google's alias for their current fast model
