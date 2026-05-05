import os
import json
import time
import requests
import psycopg2
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, BackgroundTasks
import uvicorn

load_dotenv()

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8001/fundamentals")

app = FastAPI(title="Sorting Agent API")
last_run_time = None
is_running = False

def get_db_connection():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "postgres"),
        user=os.environ.get("POSTGRES_USER", "postgres"),
        password=os.environ.get("POSTGRES_PASSWORD", "")
    )

def setup_database():
    """Create the table to store the agent's scores."""
    print("Setting up garp_momentum_scores table...")
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS garp_momentum_scores (
                    symbol TEXT PRIMARY KEY,
                    momentum_score REAL,
                    value_score REAL,
                    growth_score REAL,
                    liquidity_score REAL,
                    sentiment_score REAL,
                    roe_score REAL,
                    debt_score REAL,
                    revenue_score REAL,
                    final_score REAL,
                    short_term_score REAL,
                    long_term_score REAL,
                    rsi_14 REAL,
                    adv_30 BIGINT,
                    pe_ratio REAL,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                ALTER TABLE garp_momentum_scores ADD COLUMN IF NOT EXISTS sentiment_score REAL;
                ALTER TABLE garp_momentum_scores ADD COLUMN IF NOT EXISTS roe_score REAL;
                ALTER TABLE garp_momentum_scores ADD COLUMN IF NOT EXISTS debt_score REAL;
                ALTER TABLE garp_momentum_scores ADD COLUMN IF NOT EXISTS revenue_score REAL;
                ALTER TABLE garp_momentum_scores ADD COLUMN IF NOT EXISTS short_term_score REAL;
                ALTER TABLE garp_momentum_scores ADD COLUMN IF NOT EXISTS long_term_score REAL;
            """)
            conn.commit()
    finally:
        conn.close()

def calculate_rsi(series, period=14):
    """Calculate Relative Strength Index (RSI)."""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

API_TIMEOUT = 10  # seconds per fundamentals request


def fetch_sentiment_score(symbol: str, company_name: str, conn) -> float:
    """
    Phase 1: Query the last 7 days of news_analysis for this symbol.
    - Query 1: Matches on impact_entity using company_name (e.g. 'Reliance Industries')
               so the comparison against LLM-extracted entity names is accurate.
    - Query 2: Matches on yf_news.symbol (exact ticker) — always precise.
    Returns a score 0-100: 50 = neutral, >50 positive, <50 negative.
    """
    try:
        with conn.cursor() as cur:
            # Query 1: RSS news — match company name against LLM-extracted impact_entity
            cur.execute("""
                SELECT
                    SUM(CASE WHEN na.sentiment = 'Positive' THEN 1 ELSE 0 END) AS pos,
                    SUM(CASE WHEN na.sentiment = 'Negative' THEN 1 ELSE 0 END) AS neg,
                    COUNT(*) AS total
                FROM news_analysis na
                WHERE na.impact_level = 'Company'
                  AND UPPER(na.impact_entity) = UPPER(%s)
                  AND na.created_at > NOW() - INTERVAL '7 days'
            """, (company_name,))
            row = cur.fetchone()
            pos, neg, total = (row[0] or 0), (row[1] or 0), (row[2] or 0)

            # Query 2: yfinance news — match exact ticker symbol (always correct)
            cur.execute("""
                SELECT
                    SUM(CASE WHEN na.sentiment = 'Positive' THEN 1 ELSE 0 END) AS pos,
                    SUM(CASE WHEN na.sentiment = 'Negative' THEN 1 ELSE 0 END) AS neg,
                    COUNT(*) AS total
                FROM yf_news yf
                JOIN news_analysis na ON yf.id = na.article_id AND na.article_source = 'yf'
                WHERE UPPER(yf.symbol) = UPPER(%s)
                  AND yf.fetched_at > NOW() - INTERVAL '7 days'
            """, (symbol,))
            row2 = cur.fetchone()
            pos   += (row2[0] or 0)
            neg   += (row2[1] or 0)
            total += (row2[2] or 0)

        if total == 0:
            return 50.0  # neutral when no data
        score = ((pos - neg) / total) * 50 + 50
        return round(min(max(score, 0), 100), 2)
    except Exception as e:
        print(f"[{symbol}] Sentiment query failed: {e}")
        return 50.0


def fetch_fundamentals_from_api(ticker):
    """Call the existing API to get fundamentals."""
    print(f"[{ticker}] Fetching fundamentals from API...")
    fundamentals = {}
    try:
        bs_req   = requests.get(f"{API_BASE_URL}/{ticker}/balancesheet/annual", timeout=API_TIMEOUT)
        cf_req   = requests.get(f"{API_BASE_URL}/{ticker}/cashflow/annual",     timeout=API_TIMEOUT)
        prof_req = requests.get(f"{API_BASE_URL}/{ticker}/profile",             timeout=API_TIMEOUT)

        fundamentals['balance_sheet'] = bs_req.json()   if bs_req.status_code   == 200 else {}
        fundamentals['cash_flow']     = cf_req.json()   if cf_req.status_code   == 200 else {}
        fundamentals['profile']       = prof_req.json() if prof_req.status_code == 200 else {}
    except requests.exceptions.Timeout:
        print(f"[{ticker}] API request timed out after {API_TIMEOUT}s — fundamentals skipped")
    except requests.exceptions.RequestException as e:
        print(f"[{ticker}] API request failed: {e}")
    return fundamentals

def process_symbol(symbol, conn):  # conn is now a dedicated per-symbol connection
    print(f"[{symbol}] Processing...")

    fundamentals = fetch_fundamentals_from_api(symbol)

    # Extract company name from profile for accurate RSS sentiment matching.
    # The LLM tags news with company names (e.g. 'Reliance Industries'), not tickers.
    profile = fundamentals.get('profile', {})
    company_name = (
        profile.get('longName')
        or profile.get('shortName')
        or symbol  # last resort fallback to ticker
    )
    print(f"[{symbol}] Company name resolved to: '{company_name}'")
    
    query = """
        SELECT timestamp, COALESCE(close, adj_close) AS close, volume 
        FROM stock_prices 
        WHERE symbol = %s AND interval = '1d'
        ORDER BY timestamp ASC
    """
    df = pd.read_sql(query, conn, params=(symbol,))
    
    if len(df) < 50:
        print(f"[{symbol}] Not enough price data ({len(df)} rows). Skipping.")
        return
        
    df['rsi_14'] = calculate_rsi(df['close'], period=14)
    df['adv_30'] = df['volume'].rolling(window=30).mean()
    
    latest = df.iloc[-1]
    rsi = float(latest['rsi_14']) if not pd.isna(latest['rsi_14']) else 50.0
    adv = int(latest['adv_30']) if not pd.isna(latest['adv_30']) else 0
    
    if len(df) >= 21:
        price_1m_ago = df.iloc[-21]['close']
        momentum_pct = ((latest['close'] - price_1m_ago) / price_1m_ago) * 100
    else:
        momentum_pct = 0
        
    # --- P/E Ratio extraction ---
    # The collector's /profile endpoint returns yahooquery asset_profile which only has
    # company info (sector, country, etc.) — NOT valuation metrics.
    # trailingPe lives in yahooquery summary_detail, so we fetch it directly.
    pe_ratio = 0
    try:
        from yahooquery import Ticker as YQTicker
        yq = YQTicker(symbol)
        sd = yq.summary_detail
        if isinstance(sd, dict) and symbol in sd:
            raw_pe = sd[symbol].get('trailingPE') or sd[symbol].get('trailingPe')
            pe_ratio = float(raw_pe) if raw_pe is not None else 0
    except Exception as e:
        print(f"[{symbol}] Could not fetch trailingPe from yahooquery: {e}")
        pe_ratio = 0

    # ── Phase 1: Sentiment score ───────────────────────────────────────────────
    sentiment_score = fetch_sentiment_score(symbol, company_name, conn)

    # ── Phase 2: Additional fundamentals (ROE, Debt/Equity, Revenue Growth) ──
    balance_sheet = fundamentals.get('balance_sheet', {})
    bs_records = balance_sheet if isinstance(balance_sheet, list) else ([balance_sheet] if isinstance(balance_sheet, dict) else [])

    # --- ROE score (Return on Equity) ---
    roe_score = 50.0
    try:
        from yahooquery import Ticker as YQTicker2
        yq2 = YQTicker2(symbol)
        fs = yq2.financial_data
        if isinstance(fs, dict) and symbol in fs:
            roe_raw = fs[symbol].get('returnOnEquity')  # yahooquery returns as decimal e.g. 0.22
            if roe_raw is not None:
                roe_pct = float(roe_raw) * 100
                # 0% ROE → 0, 15% → 50, 30%+ → 100
                roe_score = min(max((roe_pct / 30) * 100, 0), 100)
    except Exception as e:
        print(f"[{symbol}] ROE fetch failed: {e}")

    # --- Debt/Equity score (lower D/E is better) ---
    debt_score = 50.0
    try:
        yq3 = YQTicker2(symbol)
        ks = yq3.key_stats
        if isinstance(ks, dict) and symbol in ks:
            de_raw = ks[symbol].get('debtToEquity')  # expressed as percentage e.g. 45.2 means 0.452
            if de_raw is not None:
                de = float(de_raw) / 100  # normalise to ratio
                # D/E 0 → 100, 0.5 → 75, 1.0 → 50, 2.0+ → 0
                debt_score = min(max(100 - (de * 50), 0), 100)
    except Exception as e:
        print(f"[{symbol}] D/E fetch failed: {e}")

    # --- Revenue Growth score ---
    revenue_score = 50.0
    try:
        yq4 = YQTicker2(symbol)
        fd = yq4.financial_data
        if isinstance(fd, dict) and symbol in fd:
            rev_growth_raw = fd[symbol].get('revenueGrowth')  # decimal e.g. 0.12 = 12%
            if rev_growth_raw is not None:
                rev_growth_pct = float(rev_growth_raw) * 100
                # -20% → 0, 0% → 50, +20%+ → 100
                revenue_score = min(max((rev_growth_pct + 20) * 2.5, 0), 100)
    except Exception as e:
        print(f"[{symbol}] Revenue growth fetch failed: {e}")

    # --- Operating Cash Flow Growth (existing logic) ---
    cash_flow = fundamentals.get('cash_flow', {})
    cf_records = cash_flow if isinstance(cash_flow, list) else ([cash_flow] if isinstance(cash_flow, dict) else [])
    ocf_values = []
    for rec in cf_records:
        if isinstance(rec, dict):
            ocf = (rec.get('OperatingCashFlow')
                   or rec.get('operatingCashflow')
                   or rec.get('totalCashFromOperatingActivities'))
            if ocf is not None:
                try:
                    ocf_values.append(float(ocf))
                except (TypeError, ValueError):
                    pass
    if len(ocf_values) >= 2:
        prior = ocf_values[1]
        if prior != 0:
            ocf_growth_pct = ((ocf_values[0] - prior) / abs(prior)) * 100
            growth_score = min(max(ocf_growth_pct + 50, 0), 100)
        else:
            growth_score = 50
    else:
        print(f"[{symbol}] No cash-flow growth data; defaulting growth_score=50")
        growth_score = 50

    momentum_score = min(max((momentum_pct + 20) * 2.5, 0), 100)
    liquidity_score = min((adv / 20000), 100)

    if 0 < pe_ratio < 15:
        value_score = 100
    elif 15 <= pe_ratio < 30:
        value_score = 100 - ((pe_ratio - 15) * 3.33)
    else:
        value_score = 20
        if pe_ratio == 0:
            print(f"[{symbol}] pe_ratio is 0 — value_score defaulting to 20")

    # ── Phase 3: Split into short-term and long-term scores ───────────────────
    # Short-term: momentum & sentiment driven (what can earn soon)
    short_term_score = (
        momentum_score   * 0.40 +
        sentiment_score  * 0.30 +
        value_score      * 0.15 +
        liquidity_score  * 0.15
    )
    # Long-term: fundamentals driven (what is worth holding)
    long_term_score = (
        roe_score        * 0.25 +
        debt_score       * 0.20 +
        revenue_score    * 0.20 +
        growth_score     * 0.20 +
        value_score      * 0.15
    )
    # Blended final score
    final_score = (short_term_score * 0.5) + (long_term_score * 0.5)

    print(
        f"[{symbol}] ST: {short_term_score:.1f} | LT: {long_term_score:.1f} | Final: {final_score:.1f} "
        f"| M:{momentum_score:.0f} V:{value_score:.0f} G:{growth_score:.0f} "
        f"| Sent:{sentiment_score:.0f} ROE:{roe_score:.0f} D/E:{debt_score:.0f} Rev:{revenue_score:.0f}"
    )
    
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO garp_momentum_scores
                (symbol, momentum_score, value_score, growth_score, liquidity_score,
                 sentiment_score, roe_score, debt_score, revenue_score,
                 final_score, short_term_score, long_term_score,
                 rsi_14, adv_30, pe_ratio, updated_at)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (symbol) DO UPDATE SET
                momentum_score   = EXCLUDED.momentum_score,
                value_score      = EXCLUDED.value_score,
                growth_score     = EXCLUDED.growth_score,
                liquidity_score  = EXCLUDED.liquidity_score,
                sentiment_score  = EXCLUDED.sentiment_score,
                roe_score        = EXCLUDED.roe_score,
                debt_score       = EXCLUDED.debt_score,
                revenue_score    = EXCLUDED.revenue_score,
                final_score      = EXCLUDED.final_score,
                short_term_score = EXCLUDED.short_term_score,
                long_term_score  = EXCLUDED.long_term_score,
                rsi_14           = EXCLUDED.rsi_14,
                adv_30           = EXCLUDED.adv_30,
                pe_ratio         = EXCLUDED.pe_ratio,
                updated_at       = NOW();
        """, (
            symbol,
            float(momentum_score), float(value_score), float(growth_score), float(liquidity_score),
            float(sentiment_score), float(roe_score), float(debt_score), float(revenue_score),
            float(final_score), float(short_term_score), float(long_term_score),
            float(rsi), adv, float(pe_ratio)
        ))
    conn.commit()

def run_agent():
    global last_run_time, is_running
    if is_running:
        print("Sorting run already in progress.")
        return

    is_running = True
    print(f"Starting scheduled SortingAgent run at {datetime.now()}...")

    # Use a dedicated connection ONLY for fetching the symbol list so that
    # per-symbol connections opened inside process_symbol don't share cursors.
    list_conn = get_db_connection()
    try:
        with list_conn.cursor() as cur:
            cur.execute("""
                SELECT symbol
                FROM (
                    SELECT symbol, COUNT(*) AS row_count
                    FROM stock_prices
                    WHERE interval = '1d'
                    GROUP BY symbol
                ) sub
                WHERE row_count >= 50
                ORDER BY symbol
            """)
            symbols = [row[0] for row in cur.fetchall()]
    finally:
        list_conn.close()

    print(f"Found {len(symbols)} symbols with >= 50 daily rows to process.")

    try:
        for symbol in symbols:
            # Open a fresh, independent connection for every symbol to avoid
            # cursor conflicts between the list query and per-symbol reads.
            sym_conn = get_db_connection()
            try:
                process_symbol(symbol, sym_conn)
            except Exception as e:
                print(f"[{symbol}] Error during processing: {e}")
                try:
                    sym_conn.rollback()
                except Exception:
                    pass
            finally:
                sym_conn.close()
            time.sleep(5)  # rate-limit API + DB calls

        print("Scoring run complete!")
        last_run_time = datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()
    except Exception as e:
        print(f"Error during agent run: {e}")
    finally:
        is_running = False

@app.get("/status")
def get_status():
    """Returns the current status of the agent and when it last ran."""
    return {
        "status": "running",
        "is_sorting_now": is_running,
        "last_sorting_run": last_run_time,
        "current_time_ist": datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()
    }

@app.post("/trigger")
def trigger_sorting(background_tasks: BackgroundTasks):
    """Manually trigger a sorting run immediately."""
    if is_running:
        return {"message": "Sorting run is already in progress"}
    background_tasks.add_task(run_agent)
    return {"message": "Sorting run triggered in background"}

@app.on_event("startup")
def startup_event():
    setup_database()
    print("Starting SortingAgent Scheduler...")
    scheduler = BackgroundScheduler(timezone=pytz.timezone("Asia/Kolkata"))
    scheduler.add_job(
        run_agent,
        trigger=CronTrigger(hour=21, minute=0, day_of_week='mon-fri'),
        id="run_sorting_agent"
    )
    scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8002"))
    uvicorn.run(app, host="0.0.0.0", port=port)
