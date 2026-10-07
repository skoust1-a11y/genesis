# Trading Strategy Lab

Ingest trading strategies from YouTube, Instagram, documents, or Markdown, then run a **Review Crew** that backtests and forward-tests scenarios with Indian options charges and returns a net profitability verdict.

## Setup

```bash
cp .env.example .env
# Add OPENAI_API_KEY for live LLM extract (optional — demo fixture works offline)
```

### Backend

```bash
cd backend
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8841
```

### Frontend

```bash
cd frontend
npm install
npm run dev -- -p 8842
```

Open http://127.0.0.1:8842

## Config precedence

- **LLM keys**: `.env` only
- **Costs / size**: strategy `.md` frontmatter overrides `.env` defaults

## Demo

Use **Run demo fixture** in the UI or `POST /api/demo/run` on the backend.
