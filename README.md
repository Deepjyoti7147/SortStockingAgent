# 📈 SortStockingAgent (Sorting Agent)

The Sorting Agent is a scheduled background service that computes composite financial scores (0–100) for Indian stocks. It pulls historical End-of-Day (EOD) price data from PostgreSQL, fetches fundamentals from the `StockmarketDataCollector` API, enriches scores with live data from `yahooquery`, and blends in news sentiment from the `NewsAnalysisAgent` database.

It produces **two separate scores per stock** — short-term and long-term — using a **GARP + Momentum + Sentiment** hybrid model.

---

## 🧠 Scoring Methodology

Each stock receives three scores, all on a 0–100 scale:

### Short-Term Score (momentum & sentiment driven)
| Metric | Weight | Source |
|--------|--------|--------|
| Momentum (1-month price change) | 40% | `stock_prices` DB |
| Sentiment (news + sector) | 30% | `NewsAnalysisAgent` DB |
| Value (P/E ratio) | 15% | yahooquery |
| Liquidity (30-day ADV) | 15% | `stock_prices` DB |

### Long-Term Score (fundamentals driven)
| Metric | Weight | Source |
|--------|--------|--------|
| ROE (Return on Equity) | 25% | yahooquery |
| Debt/Equity ratio | 20% | yahooquery → balance sheet fallback |
| Revenue Growth | 20% | yahooquery |
| Operating Cash Flow Growth | 20% | Fundamentals API (annual → quarterly → NetIncome YoY) |
| Value (P/E ratio) | 15% | yahooquery |

### Final Score
```
final_score = short_term_score × 0.5 + long_term_score × 0.5
```

All scores are stored in the `garp_momentum_scores` table.

---

## 🗄️ Database Architecture

The agent connects to **two separate PostgreSQL databases**:

| Variable Prefix | Database | Tables Used |
|-----------------|----------|-------------|
| `POSTGRES_*` | Stock data DB | `stock_prices`, `garp_momentum_scores` |
| `NEWS_POSTGRES_DB` | News/Sentiment DB | `news_analysis`, `yf_news` |

Both databases share the same host, user, and password — only the DB name differs.

---

## ⚙️ Configuration

Create a `.env` file in the root directory (never commit this to git):

```env
# Stock Price / Scoring DB
POSTGRES_HOST=your_server_ip
POSTGRES_DB=stockdata
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_secure_password

# News / Sentiment DB (same host/user/password, different DB name)
NEWS_POSTGRES_DB=newsdb

# StockmarketDataCollector API
API_BASE_URL=http://your_server_ip:8001/fundamentals
```

---

## 📡 API Endpoints

The agent runs a FastAPI server on port `8002`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/status` | Returns agent status and last run timestamp |
| `POST` | `/trigger` | Manually triggers a scoring run in the background |

---

## 🚀 Running Locally

Ensure your `StockmarketDataCollector` API is running on port `8001`.

```bash
# Install dependencies
pip install -r requirements.txt

# Run pre-deploy validation (recommended)
python preflight_check.py AARTIIND.NS

# Run the agent (starts the scheduler + API server)
python main.py
```

> The agent automatically runs at **9:00 PM IST on Weekdays** via APScheduler.

---

## 🔍 Diagnostic Scripts

| Script | Purpose |
|--------|---------|
| `preflight_check.py [SYMBOL]` | Full pre-deploy validation — tests DB connections, all API endpoints, yahooquery, and data completeness |
| `diagnose_yq.py [SYMBOL]` | Inspects raw yahooquery values (PE, ROE, D/E, Revenue Growth) vs computed scores for a single symbol |

---

## ☁️ Deployment (CI/CD)

Push to `main` to trigger the automated GitHub Actions pipeline:
1. SSH into the VM
2. Injects `.env` from GitHub Secrets
3. Pulls latest code
4. Rebuilds Docker image and restarts the container

**Required GitHub Secrets:**

| Secret | Description |
|--------|-------------|
| `SERVER_IP` | VM IP address |
| `SERVER_SSH` | SSH private key |
| `SERVER_USER` | SSH username |
| `POSTGRES_HOST` | Stock DB host |
| `POSTGRES_DB` | Stock DB name |
| `POSTGRES_USER` | DB user |
| `POSTGRES_PASSWORD` | DB password |
| `NEWS_POSTGRES_DB` | News/Sentiment DB name |
| `API_BASE_URL` | Fundamentals API base URL |
