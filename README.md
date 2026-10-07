# Trading Strategy Lab

Ingest trading strategies from YouTube, Instagram, documents, or Markdown, then run a **Review Crew** that backtests and forward-tests scenarios with Indian options charges and returns a net profitability verdict.

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

Run `uv run alembic upgrade head` again after pulling changes that add migrations. Strategies, reports, trades, reviews and the audit log live in the database set by `DATABASE_URL` (SQLite at `backend/data/genesis.db` by default; Postgres works too). See [backend/README.md](backend/README.md) for the data model, audit trail and API.

### Frontend

```bash
cd frontend
npm install
npm run dev -- -p 8842
```

Open http://127.0.0.1:8842

## Config precedence

- **LLM keys and `DATABASE_URL`**: `.env` only
- **Costs / size**: strategy `.md` frontmatter overrides `.env` defaults

## Demo

Use **Run demo fixture** in the UI or `POST /api/demo/run` on the backend.
