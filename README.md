# News-Driven Paper Trading Bot (Real Alpaca Paper Account)

Trades the S&P 500 based on real news, through your real Alpaca **paper
trading** account — real broker infrastructure, real order execution
mechanics, fake money. Free the whole way through.

## How it works
1. `news.py` pulls recent market news from Finnhub and checks which S&P 500
   companies are mentioned.
2. `decide.py` sends that news + your current Alpaca paper account state
   (cash, holdings, total value) to Google's free Gemini API, which returns
   buy/sell decisions as JSON.
3. `trader.py` submits real market orders to your Alpaca **paper trading**
   account (`paper=True` — no real money is ever involved) and reads your
   real account balance/positions directly from Alpaca.
4. `main.py` runs the whole flow once and logs what happened to `logs/`.

Your account balance and holdings live entirely in your Alpaca dashboard now
— log in at app.alpaca.markets any time to see your positions, order
history, and P&L with their normal charts and UI.

## Setup (10 minutes)

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
   (If you're on a system that blocks global installs, use
   `pip install -r requirements.txt --break-system-packages`.)

2. Open `config.py` and paste in your API keys:
   - `FINNHUB_API_KEY` — from https://finnhub.io/dashboard
   - `GEMINI_API_KEY` — from https://aistudio.google.com/apikey
   - `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` — from the **Paper Trading**
     view of https://app.alpaca.markets (API Keys panel). Do NOT use live
     trading keys.

3. Test it manually:
   ```
   python3 main.py
   ```
   You should see output like "Alpaca paper account value: $100,000.00",
   "Found N companies mentioned in current news," and a summary of any
   orders submitted. Check `logs/` for a full run history, and your Alpaca
   dashboard for the actual order book and fills.

## Running it automatically (while you sleep / during market hours)

Use `cron` (Mac/Linux) to run `main.py` every hour, 9am–4pm, Monday–Friday:

1. Open your crontab:
   ```
   crontab -e
   ```
2. Add this line (adjust the path to wherever you put this folder):
   ```
   0 9-16 * * 1-5 cd /full/path/to/trading-bot && /usr/bin/python3 main.py >> logs/cron.log 2>&1
   ```
   This runs at the top of every hour from 9am to 4pm, weekdays only.

   Note: this uses your local system time, not necessarily US market time —
   adjust the hour range if you're not in US Eastern time.

3. On Windows, use Task Scheduler instead: create a task that runs
   `python main.py` from this folder, triggered hourly on a weekday schedule.

## Files
- `config.py` — your API keys and bot settings (position size caps, etc.)
- `sp500_data.py` — auto-generated list of all 503 current S&P 500 tickers/names
- `news.py` — fetches and matches news to companies
- `decide.py` — asks Gemini for trade decisions
- `trader.py` — submits real orders to Alpaca's paper trading API
- `main.py` — runs the full cycle once (this is what cron calls)
- `logs/` — a dated log file per day, with every decision and order recorded

## Adjustable settings (in `config.py`)
- `MAX_POSITION_PCT` — max % of portfolio in any one stock (default 10%,
  enforces diversification)
- `MAX_NEWS_ITEMS` — how many headlines get sent to Gemini per run
- `GEMINI_MODEL` — which free Gemini model to use
- `STARTING_CASH` is no longer used — Alpaca sets your paper account's
  starting balance (usually $100,000, resettable from your dashboard).

## Honest limitations of this v1
- Orders are submitted as market orders during the DAY session — if
  markets are closed when the script runs, Alpaca will queue the order
  for the next open rather than reject it.
- News-to-company matching is a simple text-matching heuristic, not
  perfect entity recognition — it can occasionally miss or misfire.
- This is a real trading strategy you're testing, not a guaranteed
  money-maker. Treat the paper-trading results as a learning tool, not
  a promise of how real trading would go.
