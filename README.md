# Trading Strategy Lab

Ingest a strategy from YouTube, Instagram, an HTML page, or Markdown. The **Review Crew** backtests and forward-tests it with Indian options charges and writes an append-only report. Approving one exact version freezes it for a **Trade Crew**, which can paper-trade that copy or, when the live gates pass, trade it through a broker adapter. An LLM extracts and comments. It does not open or close a trade.

Factual architecture, schema, and dependency notes are in [DocumentsAndDiagrams/](DocumentsAndDiagrams/Readme.md). Migrations, the API, and the live runbook are in [backend/README.md](backend/README.md).

## Setup

```bash
cp .env.example .env
# Pick LLM_PROVIDER=openai (OPENAI_API_KEY) or anthropic (ANTHROPIC_API_KEY, Claude) for live LLM extract
# (optional — demo fixture works offline)
```

### Backend

```bash
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --host 0.0.0.0 --port 8841
```

Run `uv run alembic upgrade head` again after pulling changes that add migrations. Also migrate the live database before paper or live routes:

```bash
uv run alembic -n live upgrade head
```

Strategies, reports, trades, reviews and the research audit log live in `DATABASE_URL` (SQLite at `backend/data/genesis.db` by default; Postgres works too). Paper and live rows live in `LIVE_DATABASE_URL`. NSE reference data lives in `MARKET_DATABASE_URL`. See [backend/README.md](backend/README.md).

### Frontend

```bash
cd frontend
npm install
npm run dev -- -p 8842
```

Open http://127.0.0.1:8842

## Config precedence

- **Keys, database URLs, and process settings**: `.env`. Unset variables use the defaults in `backend/app/config.py`. A copied `.env.example` overrides several of those defaults (capital, lots, portfolio caps, common risk rules).
- **Costs / size**: strategy `.md` frontmatter overrides `.env` defaults.
- **Common risk rules**: the stricter of the `.env` value and the strategy `risk:` value wins.

## Demo

Use **Run demo fixture** in the UI or `POST /api/demo/run` on the backend.
