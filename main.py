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
                    final_score REAL,
                    rsi_14 REAL,
                    adv_30 BIGINT,
                    pe_ratio REAL,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
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

    # --- Cash flow / Growth score ---
    # /cashflow/annual returns yahooquery cash_flow() records.
    # yahooquery uses PascalCase: 'OperatingCashFlow' (not 'operatingCashflow').
    cash_flow = fundamentals.get('cash_flow', {})
    cf_records = cash_flow if isinstance(cash_flow, list) else ([cash_flow] if isinstance(cash_flow, dict) else [])
    ocf_values = []
    for rec in cf_records:
        if isinstance(rec, dict):
            # yahooquery PascalCase first, then camelCase fallbacks
            ocf = (rec.get('OperatingCashFlow')
                   or rec.get('operatingCashflow')
                   or rec.get('totalCashFromOperatingActivities'))
            if ocf is not None:
                try:
                    ocf_values.append(float(ocf))
                except (TypeError, ValueError):
                    pass
    if len(ocf_values) >= 2:
        # ocf_values[0] = most recent year, ocf_values[1] = prior year
        prior = ocf_values[1]
        if prior != 0:
            ocf_growth_pct = ((ocf_values[0] - prior) / abs(prior)) * 100
            # Map -50% → 0, 0% → 50, +50% → 100
            growth_score = min(max(ocf_growth_pct + 50, 0), 100)
        else:
            growth_score = 50
    else:
        print(f"[{symbol}] No cash-flow growth data available; defaulting growth_score=50")
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
            print(f"[{symbol}] pe_ratio is 0 (missing data) — value_score defaulting to 20")
    final_score = (momentum_score * 0.4) + (value_score * 0.2) + (growth_score * 0.2) + (liquidity_score * 0.2)
    
    print(f"[{symbol}] Scores -> Final: {final_score:.2f} | M: {momentum_score:.1f} | V: {value_score:.1f} | L: {liquidity_score:.1f}")
    
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO garp_momentum_scores 
                (symbol, momentum_score, value_score, growth_score, liquidity_score, final_score, rsi_14, adv_30, pe_ratio, updated_at)
            VALUES 
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (symbol) DO UPDATE SET
                momentum_score = EXCLUDED.momentum_score,
                value_score = EXCLUDED.value_score,
                growth_score = EXCLUDED.growth_score,
                liquidity_score = EXCLUDED.liquidity_score,
                final_score = EXCLUDED.final_score,
                rsi_14 = EXCLUDED.rsi_14,
                adv_30 = EXCLUDED.adv_30,
                pe_ratio = EXCLUDED.pe_ratio,
                updated_at = NOW();
        """, (
            symbol, float(momentum_score), float(value_score), float(growth_score), float(liquidity_score), 
            float(final_score), float(rsi), adv, float(pe_ratio)
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
