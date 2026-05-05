"""
Diagnose yahooquery raw values vs computed scores for a given symbol.
Run: python diagnose_yq.py [SYMBOL]
Default symbol: 360ONE.NS
"""
import sys
from yahooquery import Ticker

SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "360ONE.NS"

print(f"\n{'='*60}")
print(f"  yahooquery diagnostics for: {SYMBOL}")
print(f"{'='*60}\n")

yq = Ticker(SYMBOL)

# ── 1. P/E Ratio ─────────────────────────────────────────────
print("── 1. P/E Ratio (summary_detail) ──")
try:
    sd = yq.summary_detail
    if isinstance(sd, dict) and SYMBOL in sd:
        raw_pe = sd[SYMBOL].get('trailingPE') or sd[SYMBOL].get('trailingPe')
        pe_ratio = float(raw_pe) if raw_pe is not None else 0
        print(f"   trailingPE raw   : {raw_pe}")
        print(f"   pe_ratio used    : {pe_ratio}")
        if 0 < pe_ratio < 15:
            value_score = 100
        elif 15 <= pe_ratio < 30:
            value_score = 100 - ((pe_ratio - 15) * 3.33)
        else:
            value_score = 20
        print(f"   → value_score    : {value_score:.2f}")
    else:
        print(f"   ERROR: symbol not in response → {sd}")
except Exception as e:
    print(f"   EXCEPTION: {e}")

# ── 2. ROE ────────────────────────────────────────────────────
print("\n── 2. ROE (financial_data.returnOnEquity) ──")
try:
    fd = yq.financial_data
    if isinstance(fd, dict) and SYMBOL in fd:
        roe_raw = fd[SYMBOL].get('returnOnEquity')
        print(f"   returnOnEquity raw: {roe_raw}")
        if roe_raw is not None:
            roe_pct = float(roe_raw) * 100
            roe_score = min(max((roe_pct / 30) * 100, 0), 100)
            print(f"   roe_pct           : {roe_pct:.2f}%")
            print(f"   → roe_score       : {roe_score:.2f}")
        else:
            print("   returnOnEquity is None → roe_score defaults to 50")
    else:
        print(f"   ERROR: symbol not in response → {fd}")
except Exception as e:
    print(f"   EXCEPTION: {e}")

# ── 3. Debt/Equity ────────────────────────────────────────────
print("\n── 3. Debt/Equity (key_stats.debtToEquity) ──")
try:
    ks = yq.key_stats
    if isinstance(ks, dict) and SYMBOL in ks:
        de_raw = ks[SYMBOL].get('debtToEquity')
        print(f"   debtToEquity raw  : {de_raw}")
        if de_raw is not None:
            de = float(de_raw) / 100
            debt_score = min(max(100 - (de * 50), 0), 100)
            print(f"   D/E ratio         : {de:.4f}")
            print(f"   → debt_score      : {debt_score:.2f}")
        else:
            print("   debtToEquity is None → debt_score defaults to 50")
    else:
        print(f"   ERROR: symbol not in response → {ks}")
except Exception as e:
    print(f"   EXCEPTION: {e}")

# ── 4. Revenue Growth ─────────────────────────────────────────
print("\n── 4. Revenue Growth (financial_data.revenueGrowth) ──")
try:
    fd2 = yq.financial_data
    if isinstance(fd2, dict) and SYMBOL in fd2:
        rev_raw = fd2[SYMBOL].get('revenueGrowth')
        print(f"   revenueGrowth raw : {rev_raw}")
        if rev_raw is not None:
            rev_pct = float(rev_raw) * 100
            revenue_score = min(max((rev_pct + 20) * 2.5, 0), 100)
            print(f"   revenue growth    : {rev_pct:.2f}%")
            print(f"   → revenue_score   : {revenue_score:.2f}")
        else:
            print("   revenueGrowth is None → revenue_score defaults to 50")
    else:
        print(f"   ERROR: symbol not in response → {fd2}")
except Exception as e:
    print(f"   EXCEPTION: {e}")

# ── 5. Full key_stats dump (for inspection) ──────────────────
print("\n── 5. All key_stats fields available ──")
try:
    ks2 = yq.key_stats
    if isinstance(ks2, dict) and SYMBOL in ks2:
        for k, v in sorted(ks2[SYMBOL].items()):
            print(f"   {k:40s}: {v}")
    else:
        print(f"   ERROR: {ks2}")
except Exception as e:
    print(f"   EXCEPTION: {e}")

print(f"\n{'='*60}\n")
