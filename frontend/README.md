# Trading Strategy Lab — frontend

Next.js 15 + React 19 + Tailwind 4. Talks to the backend at `NEXT_PUBLIC_API_URL` (default `http://127.0.0.1:8841`).

```bash
npm install
npm run dev -- -p 8842
```

Use port 8842 so the app matches `CORS_ORIGINS` in the repo-root `.env.example`. `npm run dev` with no port uses Next.js's default of 3000.

Pages:

- `/` — the lab: ingest a strategy, run reports, browse trades, including the hard-stop sweep on a report.
- `/deploy` — pick a strategy version and a broker account, set the trade management policy (sizing, compounding, profit booking, time and event exits, daily target, VIX sizing, withdrawal rule, odds gate), the drawdown ladder, and the capital floor, backtest rules only against rules with the policy, then approve for paper or live.
- `/deployments` — start, pause, stop, or stop-and-flatten a deployment. Shows equity, the ladder, open positions, deposits and withdrawals, the capital floor, withdrawal due, the odds table, and whether the crew is running.

The deploy page stores an API token in `localStorage` (`tsl.token`). Requests send it as `Authorization: Bearer`. Your name is stored as `tsl.actor` and sent as `X-Actor` when no token is in use. See `backend/README.md` for which actions require a token.
