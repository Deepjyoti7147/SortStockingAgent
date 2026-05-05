# -*- coding: utf-8 -*-
"""
diagnose_scores.py
Checks all inputs needed to compute each score for 5 random symbols.
"""
import os, sys, requests, psycopg2
from dotenv import load_dotenv

# Force UTF-8 output on Windows
sys.stdout.reconfigure(encoding='utf-8')

load_dotenv()

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8001/fundamentals")
TIMEOUT = 10  # seconds per API call

def get_conn():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "postgres"),
        user=os.environ.get("POSTGRES_USER", "postgres"),
        password=os.environ.get("POSTGRES_PASSWORD", "")
    )

def check_symbol(symbol):
    print(f"\n{'='*60}")
    print(f"  SYMBOL: {symbol}")
    print(f"{'='*60}")

    # ── 1. Price data from DB (using cursor, not pd.read_sql) ──
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT timestamp, COALESCE(close, adj_close) AS close, volume "
            "FROM stock_prices "
            "WHERE symbol=%s AND interval='1d' ORDER BY timestamp ASC",
            (symbol,)
        )
        rows = cur.fetchall()
    conn.close()

    n = len(rows)
    print(f"\n[DB] stock_prices rows      : {n}")
    if n > 0:
        print(f"[DB] Date range             : {rows[0][0]} to {rows[-1][0]}")
        latest_close  = rows[-1][1]
        latest_volume = rows[-1][2]
        print(f"[DB] Latest close           : {latest_close}")
        print(f"[DB] Latest volume          : {latest_volume}")

        # ADV-30 (liquidity)
        if n >= 30:
            vols = [r[2] for r in rows[-30:] if r[2] is not None]
            adv_30 = sum(vols) / len(vols) if vols else 0
            liq_score = min(adv_30 / 20000, 100)
            print(f"[DB] ADV-30                 : {adv_30:,.0f}  => liquidity_score = {liq_score:.1f}")
        else:
            print(f"[DB] WARNING: <30 rows, ADV-30 unreliable")

        # Momentum (1-month)
        if latest_close is None:
            print(f"[DB] WARNING: latest close is NULL - momentum cannot be computed")
        elif n >= 21:
            price_21d_ago = rows[-21][1]
            if price_21d_ago and price_21d_ago != 0:
                mom_pct = ((latest_close - price_21d_ago) / price_21d_ago) * 100
                mom_score = min(max((mom_pct + 20) * 2.5, 0), 100)
                print(f"[DB] 1-month momentum       : {mom_pct:.2f}%  => momentum_score = {mom_score:.1f}")
            else:
                print(f"[DB] WARNING: price 21d ago is 0 or None")
        else:
            print(f"[DB] WARNING: <21 rows, momentum defaults to 0")
    else:
        print(f"[DB] ERROR: No price data found!")

    # ── 2. P/E from yahooquery summary_detail (not /profile which has no valuation data) ──
    print()
    try:
        from yahooquery import Ticker as YQTicker
        yq = YQTicker(symbol)
        sd = yq.summary_detail
        if isinstance(sd, dict) and symbol in sd:
            raw_pe = sd[symbol].get('trailingPE') or sd[symbol].get('trailingPe')
            print(f"[YQ summary_detail] trailingPE  : {raw_pe}")
            if raw_pe:
                pe_f = float(raw_pe)
                if 0 < pe_f < 15:     vs = 100
                elif 15 <= pe_f < 30: vs = 100 - ((pe_f - 15) * 3.33)
                else:                 vs = 20
                print(f"[YQ summary_detail] value_score : {vs:.1f}")
            else:
                print(f"[YQ summary_detail] WARNING: trailingPE missing => value_score = 20 (default)")
            print(f"[YQ summary_detail] Keys present: {list(sd[symbol].keys())[:12]}")
        else:
            print(f"[YQ summary_detail] ERROR: symbol not in response or bad format: {type(sd)}")
    except Exception as e:
        print(f"[YQ summary_detail] ERROR: {e}")

    # ── 3. /profile from collector API (company info only, for reference) ──
    print()
    try:
        r = requests.get(f"{API_BASE_URL}/{symbol}/profile", timeout=TIMEOUT)
        print(f"[API /profile] HTTP         : {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            record = data[0] if isinstance(data, list) and data else data
            if isinstance(record, dict):
                print(f"[API /profile] sector        : {record.get('sector','N/A')}")
                print(f"[API /profile] industry      : {record.get('industry','N/A')}")
    except requests.exceptions.Timeout:
        print(f"[API /profile] ERROR: timed out")
    except Exception as e:
        print(f"[API /profile] ERROR: {e}")

    # ── 3. Cashflow / Growth from API ──
    print()
    try:
        r = requests.get(f"{API_BASE_URL}/{symbol}/cashflow/annual", timeout=TIMEOUT)
        print(f"[API /cashflow] HTTP        : {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            records = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
            print(f"[API /cashflow] Records     : {len(records)}")
            ocf_values = []
            for rec in records:
                if isinstance(rec, dict):
                    # yahooquery PascalCase: OperatingCashFlow
                    ocf = (rec.get('OperatingCashFlow')
                           or rec.get('operatingCashflow')
                           or rec.get('totalCashFromOperatingActivities'))
                    if ocf is not None:
                        try: ocf_values.append(float(ocf))
                        except: pass
            print(f"[API /cashflow] OCF values  : {ocf_values[:4]}")
            if len(ocf_values) >= 2:
                prior = ocf_values[1]
                if prior != 0:
                    g_pct = ((ocf_values[0] - prior) / abs(prior)) * 100
                    gs = min(max(g_pct + 50, 0), 100)
                    print(f"[API /cashflow] YoY growth  : {g_pct:.2f}%  => growth_score = {gs:.1f}")
                else:
                    print(f"[API /cashflow] WARNING: Prior year OCF=0 => growth_score = 50 (default)")
            else:
                print(f"[API /cashflow] WARNING: Need >=2 OCF values, got {len(ocf_values)} => growth_score = 50 (default)")
            if records and isinstance(records[0], dict):
                print(f"[API /cashflow] Keys present: {list(records[0].keys())[:12]}")
        else:
            print(f"[API /cashflow] ERROR body  : {r.text[:300]}")
    except requests.exceptions.Timeout:
        print(f"[API /cashflow] ERROR: Request timed out after {TIMEOUT}s")
    except Exception as e:
        print(f"[API /cashflow] ERROR: {e}")


# ── Pick 5 random symbols with >= 50 rows ──
conn = get_conn()
with conn.cursor() as cur:
    cur.execute("""
        SELECT symbol FROM (
            SELECT symbol, COUNT(*) AS cnt
            FROM stock_prices WHERE interval='1d'
            GROUP BY symbol
        ) sub WHERE cnt >= 50
        ORDER BY RANDOM() LIMIT 5
    """)
    sample_symbols = [r[0] for r in cur.fetchall()]
conn.close()

print(f"Sampled symbols: {sample_symbols}")
for sym in sample_symbols:
    check_symbol(sym)

print(f"\n{'='*60}")
print("Diagnosis complete.")
