"""
Pre-deploy validation for SortingAgent.
Tests every data source for a sample symbol before you deploy.
Run: python preflight_check.py [SYMBOL]
Default: AARTIIND.NS
"""
import os, sys, requests, psycopg2
from dotenv import load_dotenv

load_dotenv()

SYMBOL      = sys.argv[1] if len(sys.argv) > 1 else "AARTIIND.NS"
API_BASE    = os.environ.get("API_BASE_URL", "http://localhost:8001/fundamentals")
TIMEOUT     = 10

PASS = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"

errors = []

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

def check(label, ok, detail=""):
    tag = PASS if ok else FAIL
    print(f"  {tag}  {label}" + (f"  ->  {detail}" if detail else ""))
    if not ok:
        errors.append(label)

# ─────────────────────────────────────────────────────────────
# 1. Stock DB connection
# ─────────────────────────────────────────────────────────────
section("1. Stock DB Connection (POSTGRES_*)")
try:
    conn = psycopg2.connect(
        host=os.environ["POSTGRES_HOST"],
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )
    check("Connected to stock DB", True,
          f"{os.environ['POSTGRES_HOST']} / {os.environ['POSTGRES_DB']}")

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM stock_prices WHERE interval='1d' AND symbol=%s", (SYMBOL,))
        rows = cur.fetchone()[0]
        check(f"stock_prices rows for {SYMBOL}", rows >= 50, f"{rows} rows (need >=50)")

        cur.execute("SELECT COUNT(*) FROM stock_prices WHERE interval='1d'")
        total = cur.fetchone()[0]
        check("stock_prices total rows", total > 0, f"{total:,} rows")

        cur.execute("""
            SELECT COUNT(*) FROM (
                SELECT symbol FROM stock_prices WHERE interval='1d'
                GROUP BY symbol HAVING COUNT(*) >= 50
            ) s
        """)
        eligible = cur.fetchone()[0]
        check("Symbols eligible for scoring", eligible > 0, f"{eligible} symbols")

    conn.close()
except Exception as e:
    check("Stock DB connection", False, str(e))

# ─────────────────────────────────────────────────────────────
# 2. News DB connection
# ─────────────────────────────────────────────────────────────
section("2. News DB Connection (NEWS_POSTGRES_DB)")
news_db = os.environ.get("NEWS_POSTGRES_DB", "")
check("NEWS_POSTGRES_DB is set", bool(news_db), news_db or "MISSING")

if news_db:
    try:
        nconn = psycopg2.connect(
            host=os.environ["POSTGRES_HOST"],
            port=os.environ.get("POSTGRES_PORT", "5432"),
            dbname=news_db,
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
        )
        check("Connected to news DB", True, f"{os.environ['POSTGRES_HOST']} / {news_db}")

        with nconn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'news_analysis'
                )
            """)
            na_exists = cur.fetchone()[0]
            check("news_analysis table exists", na_exists)

            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'yf_news'
                )
            """)
            yf_exists = cur.fetchone()[0]
            check("yf_news table exists", yf_exists)

            if na_exists:
                cur.execute("SELECT COUNT(*) FROM news_analysis")
                na_rows = cur.fetchone()[0]
                check("news_analysis has data", na_rows > 0, f"{na_rows:,} rows")

        nconn.close()
    except Exception as e:
        check("News DB connection", False, str(e))

# ─────────────────────────────────────────────────────────────
# 3. Fundamentals API endpoints
# ─────────────────────────────────────────────────────────────
section(f"3. Fundamentals API  ({API_BASE})")

endpoints = {
    "balance sheet (annual)":   f"{API_BASE}/{SYMBOL}/balancesheet/annual",
    "cash flow (annual)":        f"{API_BASE}/{SYMBOL}/cashflow/annual",
    "cash flow (quarterly)":     f"{API_BASE}/{SYMBOL}/cashflow/quarterly",
    "profile":                   f"{API_BASE}/{SYMBOL}/profile",
}

api_data = {}
for label, url in endpoints.items():
    try:
        r = requests.get(url, timeout=TIMEOUT)
        ok = r.status_code == 200
        data = r.json() if ok else None
        records = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        check(f"GET {label}", ok, f"{len(records)} record(s)  [HTTP {r.status_code}]")
        api_data[label] = records
    except Exception as e:
        check(f"GET {label}", False, str(e))
        api_data[label] = []

# Spot-check key fields
bs = api_data.get("balance sheet (annual)", [])
if bs:
    latest = max(bs, key=lambda r: r.get('asOfDate', ''))
    td = latest.get('TotalDebt')
    eq = latest.get('CommonStockEquity') or latest.get('StockholdersEquity')
    check("Balance sheet has TotalDebt", td is not None, str(td))
    check("Balance sheet has CommonStockEquity", eq is not None, str(eq))
    if td is not None and eq and float(eq) > 0:
        de = float(td) / float(eq)
        print(f"         Computed D/E = {de:.3f}  ->  debt_score = {min(max(100-(de*50),0),100):.1f}")

cf_ann = api_data.get("cash flow (annual)", [])
ocf_ann = [(r.get('asOfDate',''), r.get('OperatingCashFlow') or r.get('operatingCashflow'))
           for r in cf_ann if r.get('OperatingCashFlow') or r.get('operatingCashflow')]
check("Annual cash flow has OCF data", len(ocf_ann) >= 2,
      f"{len(ocf_ann)} record(s) with OCF" + (f"  latest={ocf_ann[0][1]:,.0f}" if ocf_ann else ""))

cf_qtr = api_data.get("cash flow (quarterly)", [])
ocf_qtr = [(r.get('asOfDate',''), r.get('OperatingCashFlow') or r.get('operatingCashflow'))
           for r in cf_qtr if r.get('OperatingCashFlow') or r.get('operatingCashflow')]
check("Quarterly cash flow has OCF data", len(ocf_qtr) >= 2,
      f"{len(ocf_qtr)} record(s) with OCF" + (f"  latest={ocf_qtr[0][1]:,.0f}" if ocf_qtr else ""))

# NetIncome fallback availability
all_cf = cf_ann + cf_qtr
ni_recs = [r for r in all_cf if isinstance(r, dict) and r.get('NetIncome') is not None]
check("NetIncome fallback available", len(ni_recs) >= 2, f"{len(ni_recs)} records with NetIncome")

prof = api_data.get("profile", [{}])
p = prof[0] if prof else {}
check("Profile has longName/shortName", bool(p.get('longName') or p.get('shortName')),
      p.get('longName') or p.get('shortName') or "MISSING")
check("Profile has sector", bool(p.get('sector')), p.get('sector') or "MISSING")

# ─────────────────────────────────────────────────────────────
# 4. yahooquery
# ─────────────────────────────────────────────────────────────
section(f"4. yahooquery  ({SYMBOL})")
try:
    from yahooquery import Ticker as YQT
    yq = YQT(SYMBOL)

    sd = yq.summary_detail
    pe = None
    if isinstance(sd, dict) and SYMBOL in sd:
        pe = sd[SYMBOL].get('trailingPE') or sd[SYMBOL].get('trailingPe')
    check("trailingPE available", pe is not None, f"{pe}")

    fd = yq.financial_data
    roe = None
    rev = None
    if isinstance(fd, dict) and SYMBOL in fd:
        roe = fd[SYMBOL].get('returnOnEquity')
        rev = fd[SYMBOL].get('revenueGrowth')
    check("returnOnEquity available", roe is not None,
          f"{roe*100:.2f}%" if roe is not None else "None")
    check("revenueGrowth available",  rev is not None,
          f"{rev*100:.2f}%" if rev is not None else "None")

    ks = yq.key_stats
    de_yq = None
    if isinstance(ks, dict) and SYMBOL in ks:
        de_yq = ks[SYMBOL].get('debtToEquity')
    if de_yq is not None:
        check("debtToEquity (yahooquery)", True, f"{de_yq}")
    else:
        print(f"  {WARN}  debtToEquity (yahooquery) = None  ->  will use balance sheet fallback")

except ImportError:
    check("yahooquery installed", False, "pip install yahooquery")
except Exception as e:
    check("yahooquery fetch", False, str(e))

# ─────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────
section("SUMMARY")
if errors:
    print(f"  {len(errors)} issue(s) found. Fix before deploying:\n")
    for e in errors:
        print(f"    {FAIL}  {e}")
else:
    print(f"  All checks passed. Safe to deploy!")
print()
