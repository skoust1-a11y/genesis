# Trading Strategy Lab — frontend

Next.js 15 + React 19 + Tailwind 4. Talks to the backend at `NEXT_PUBLIC_API_URL` (default `http://localhost:8841`).

```bash
npm install
npm run dev        # http://localhost:3000
```

Pages:

- `/` — the lab: ingest a strategy, run reports, browse trades.
- `/deploy` — pick a strategy version, set the trade management policy (sizing, compounding, profit booking, time and event exits, daily profit target) and the drawdown ladder, backtest rules only against rules with the policy, then approve for paper trading.
- `/deployments` — start, pause or stop a deployment; equity, drawdown and the ladder in force, open positions, deposits and withdrawals, and the frozen risk limits.

Mutating requests send your name as `X-Actor` (asked for on the page and kept in `localStorage`). It is not authentication; see `backend/README.md`.
