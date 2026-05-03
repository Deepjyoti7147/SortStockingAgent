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

def fetch_fundamentals_from_api(ticker):
    """Call the existing API to get fundamentals."""
    print(f"[{ticker}] Fetching fundamentals from API...")
    fundamentals = {}
    try:
        bs_req = requests.get(f"{API_BASE_URL}/{ticker}/balancesheet/annual")
        cf_req = requests.get(f"{API_BASE_URL}/{ticker}/cashflow/annual")
        prof_req = requests.get(f"{API_BASE_URL}/{ticker}/profile")
        
        fundamentals['balance_sheet'] = bs_req.json() if bs_req.status_code == 200 else {}
        fundamentals['cash_flow'] = cf_req.json() if cf_req.status_code == 200 else {}
        fundamentals['profile'] = prof_req.json() if prof_req.status_code == 200 else {}
    except requests.exceptions.RequestException as e:
        print(f"[{ticker}] API request failed: {e}")
    return fundamentals

def process_symbol(symbol, conn):
    print(f"[{symbol}] Processing...")
    
    fundamentals = fetch_fundamentals_from_api(symbol)
    
    query = """
        SELECT timestamp, close, volume 
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
        
    profile = fundamentals.get('profile', {})
    if isinstance(profile, dict):
        pe_ratio = profile.get('trailingPE', 0)
    else:
        pe_ratio = 0
        
    if pe_ratio is None: pe_ratio = 0
    
    momentum_score = min(max((momentum_pct + 20) * 2.5, 0), 100) 
    liquidity_score = min((adv / 20000), 100) 
    
    if 0 < pe_ratio < 15:
        value_score = 100
    elif 15 <= pe_ratio < 30:
        value_score = 100 - ((pe_ratio - 15) * 3.33)
    else:
        value_score = 20
        
    growth_score = 50 
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
    conn = get_db_connection()
    
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT symbol FROM stock_prices")
            symbols = [row[0] for row in cur.fetchall()]
            
        print(f"Found {len(symbols)} symbols to process.")
        
        for symbol in symbols:
            process_symbol(symbol, conn)
            time.sleep(1) # Be gentle on the API and DB
            
        print("Scoring run complete!")
        last_run_time = datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()
    except Exception as e:
        print(f"Error during agent run: {e}")
    finally:
        is_running = False
        conn.close()

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
