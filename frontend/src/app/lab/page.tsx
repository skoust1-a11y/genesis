"use client";

import { useMemo, useState } from "react";

import { API, api } from "@/lib/api";

type StopWindow = { trades: number; win_rate: number; net_pnl: number };

type HardStopRow = {
  stop_pct: number;
  stop_inr: number;
  target_inr: number;
  backtest: StopWindow;
  forward: StopWindow;
  avg_loss: number | null;
  worst_loss: number | null;
  max_drawdown: number;
  sizing_blocked: number;
  eligible: boolean;
};

type HardStopSweep = {
  capital_inr: number;
  risk_reward: number | null;
  rows: HardStopRow[];
  recommended_pct: number | null;
  reason: string;
};

type Question = { id: string; question: string; why: string; required: boolean; options: string[] };

type Extracted = { strategy_id: string; markdown: string; questions: Question[] };

type WindowMetrics = {
  net_pnl?: number;
  gross_pnl?: number;
  total_costs?: number;
  trades?: number;
  skipped_signals?: Record<string, number>;
};

type ReportPayload = {
  strategy_id: string;
  markdown?: string;
  report: {
    verdict?: { label?: string; scenarios_passing?: number; scenarios_tested?: number };
    narrative?: string;
    scenario_results?: Array<{
      pass_net?: boolean;
      edge_before_costs?: boolean;
      too_few_trades?: boolean;
      backtest?: WindowMetrics;
      forward?: WindowMetrics;
    }>;
    cost_summary?: Record<string, unknown>;
    hard_stop_sweep?: HardStopSweep;
  };
};

const inr = (v: number | null | undefined) => (v == null ? "—" : `₹${Math.round(v).toLocaleString("en-IN")}`);
const pct = (v: number) => `${Math.round(v * 100)}%`;

function HardStopTable({ sweep }: { sweep: HardStopSweep }) {
  return (
    <div className="space-y-2">
      <h3 className="font-medium">Hard stop-loss (% of capital)</h3>
      <p className="text-xs text-zinc-500">
        On {inr(sweep.capital_inr)} capital, target{" "}
        {sweep.risk_reward != null ? `${sweep.risk_reward.toFixed(2)} × the stop (the strategy's ratio)` : "—"}. Daily
        closes: a gap day can lose more than the stop, as the worst-loss column shows.
      </p>
      {sweep.rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="min-w-full text-sm">
            <thead>
              <tr className="border-b text-left">
                <th className="py-2 pr-4">Stop</th>
                <th className="py-2 pr-4">₹ stop / ₹ target</th>
                <th className="py-2 pr-4">Backtest trades</th>
                <th className="py-2 pr-4">Win rate</th>
                <th className="py-2 pr-4">Net</th>
                <th className="py-2 pr-4">Forward trades</th>
                <th className="py-2 pr-4">Win rate</th>
                <th className="py-2 pr-4">Net</th>
                <th className="py-2 pr-4">Worst loss</th>
                <th className="py-2 pr-4">Largest drawdown</th>
              </tr>
            </thead>
            <tbody>
              {sweep.rows.map((row) => (
                <tr
                  key={row.stop_pct}
                  className={`border-b ${row.stop_pct === sweep.recommended_pct ? "bg-emerald-50 font-medium" : ""}`}
                >
                  <td className="py-2 pr-4">{row.stop_pct}%</td>
                  <td className="py-2 pr-4">
                    {inr(row.stop_inr)} / {inr(row.target_inr)}
                  </td>
                  <td className="py-2 pr-4">{row.backtest.trades}</td>
                  <td className="py-2 pr-4">{pct(row.backtest.win_rate)}</td>
                  <td className="py-2 pr-4">{inr(row.backtest.net_pnl)}</td>
                  <td className="py-2 pr-4">{row.forward.trades}</td>
                  <td className="py-2 pr-4">{pct(row.forward.win_rate)}</td>
                  <td className="py-2 pr-4">{inr(row.forward.net_pnl)}</td>
                  <td className="py-2 pr-4">{inr(row.worst_loss)}</td>
                  <td className="py-2 pr-4">{inr(row.max_drawdown)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className={`text-sm ${sweep.recommended_pct != null ? "text-emerald-900" : "text-zinc-600"}`}>
        {sweep.recommended_pct != null ? `Recommended: ${sweep.recommended_pct}%. ` : "No stop recommended. "}
        {sweep.reason}
      </p>
    </div>
  );
}

function SkippedSetups({ results }: { results: { backtest?: WindowMetrics; forward?: WindowMetrics }[] }) {
  const totals: Record<string, number> = {};
  for (const r of results)
    for (const w of [r.backtest, r.forward])
      for (const [rule, n] of Object.entries(w?.skipped_signals ?? {})) totals[rule] = (totals[rule] ?? 0) + n;
  const rows = Object.entries(totals).sort((a, b) => b[1] - a[1]);
  return (
    <div className="space-y-1">
      <h3 className="font-medium">Setups skipped and why</h3>
      {rows.length === 0 ? (
        <p className="text-sm text-zinc-500">Every entry signal was taken: no filter said no.</p>
      ) : (
        <ul className="text-sm text-zinc-700">
          {rows.map(([rule, n]) => (
            <li key={rule}>
              <span className="font-mono text-xs">{rule}</span>: {n}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function Lab() {
  const [sourceType, setSourceType] = useState("youtube");
  const [url, setUrl] = useState("");
  const [notes, setNotes] = useState("");
  const [markdown, setMarkdown] = useState("");
  const [strategyId, setStrategyId] = useState<string | null>(null);
  const [report, setReport] = useState<ReportPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [questions, setQuestions] = useState<Question[]>([]);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [extractedMarkdown, setExtractedMarkdown] = useState("");

  const verdict = report?.report?.verdict?.label ?? "—";
  const required = questions.filter((q) => q.required);
  // Required gaps block the review until answered, unless the user fixed the markdown by hand.
  const blocked = required.length > 0 && markdown === extractedMarkdown;

  const verdictClass = useMemo(() => {
    if (verdict === "Profitable") return "bg-emerald-100 text-emerald-900";
    if (verdict === "Conditional" || verdict === "Too few trades" || verdict === "Synthetic data only")
      return "bg-amber-100 text-amber-900";
    if (verdict === "Not profitable") return "bg-rose-100 text-rose-900";
    return "bg-zinc-100 text-zinc-800";
  }, [verdict]);

  function applyExtraction(data: Extracted) {
    setStrategyId(data.strategy_id);
    setMarkdown(data.markdown);
    setExtractedMarkdown(data.markdown);
    setQuestions(data.questions ?? []);
    setAnswers({});
    setReport(null);
  }

  async function submitAnswers() {
    setLoading(true);
    setError(null);
    try {
      applyExtraction(await api<Extracted>("/api/ingest/clarify", { body: { strategy_id: strategyId, answers } }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Re-extract failed");
    } finally {
      setLoading(false);
    }
  }

  async function extractStrategy() {
    setLoading(true);
    setError(null);
    try {
      const body: Record<string, string> = { source_type: sourceType, hints: "" };
      if (sourceType === "markdown") body.text = markdown;
      else if (sourceType === "notebooklm") {
        body.text = notes;
        if (url) body.url = url;
      } else {
        body.url = url;
      }
      const res = await fetch(`${API}/api/ingest/extract`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? "Extract failed");
      applyExtraction(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Extract failed");
    } finally {
      setLoading(false);
    }
  }

  async function runReview() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API}/api/review/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ strategy_id: strategyId, markdown }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? "Review failed");
      setReport(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Review failed");
    } finally {
      setLoading(false);
    }
  }

  async function runDemo() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API}/api/demo/run`, { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? "Demo failed");
      setQuestions([]);
      setStrategyId(data.strategy_id);
      setMarkdown(data.markdown);
      setReport({ strategy_id: data.strategy_id, markdown: data.markdown, report: data.report });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Demo failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen bg-zinc-50 text-zinc-900">
      <div className="mx-auto max-w-6xl px-4 py-10 space-y-8">
        <header className="space-y-2">
          <h1 className="text-3xl font-semibold tracking-tight">Strategy Lab</h1>
          <p className="text-zinc-600 max-w-3xl">
            Ingest a strategy from YouTube, Instagram, NotebookLM notes, or Markdown. The Review Crew
            backtests and forward-tests scenarios with Indian options charges and returns a net verdict.
          </p>
        </header>

        <section className="grid gap-6 lg:grid-cols-2">
          <div className="rounded-xl border bg-white p-5 shadow-sm space-y-4">
            <h2 className="font-medium">1. Strategy source</h2>
            <select
              className="w-full rounded-md border px-3 py-2"
              value={sourceType}
              onChange={(e) => setSourceType(e.target.value)}
            >
              <option value="youtube">YouTube URL</option>
              <option value="instagram">Instagram URL</option>
              <option value="notebooklm">NotebookLM notes (paste)</option>
              <option value="document">Document URL</option>
              <option value="markdown">Markdown (paste)</option>
            </select>
            {sourceType !== "markdown" && sourceType !== "notebooklm" && (
              <input
                className="w-full rounded-md border px-3 py-2"
                placeholder="https://..."
                value={url}
                onChange={(e) => setUrl(e.target.value)}
              />
            )}
            {sourceType === "notebooklm" && (
              <>
                <input
                  className="w-full rounded-md border px-3 py-2"
                  placeholder="Optional YouTube URL reference"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                />
                <textarea
                  className="w-full min-h-32 rounded-md border px-3 py-2 font-mono text-sm"
                  placeholder="Paste NotebookLM briefing..."
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                />
              </>
            )}
            {sourceType === "markdown" && (
              <textarea
                className="w-full min-h-48 rounded-md border px-3 py-2 font-mono text-sm"
                placeholder="Paste strategy .md"
                value={markdown}
                onChange={(e) => setMarkdown(e.target.value)}
              />
            )}
            <div className="flex flex-wrap gap-2">
              <button
                className="rounded-md bg-zinc-900 text-white px-4 py-2 disabled:opacity-50"
                disabled={loading}
                onClick={extractStrategy}
              >
                Extract strategy
              </button>
              <button
                className="rounded-md border px-4 py-2 disabled:opacity-50"
                disabled={loading}
                onClick={runDemo}
              >
                Run demo fixture
              </button>
            </div>
          </div>

          <div className="rounded-xl border bg-white p-5 shadow-sm space-y-4">
            <h2 className="font-medium">2. Strategy `.md`</h2>
            <textarea
              className="w-full min-h-72 rounded-md border px-3 py-2 font-mono text-xs"
              value={markdown}
              onChange={(e) => setMarkdown(e.target.value)}
            />
            <button
              className="rounded-md bg-indigo-600 text-white px-4 py-2 disabled:opacity-50"
              disabled={loading || !markdown.trim() || blocked}
              onClick={runReview}
            >
              Run Review Crew
            </button>
            {blocked && (
              <p className="text-xs text-amber-800">
                Answer the {required.length} required question{required.length > 1 ? "s" : ""} below (or fix the
                markdown) first: a backtest on guessed rules is noise.
              </p>
            )}
          </div>
        </section>

        {questions.length > 0 && (
          <section className="rounded-xl border bg-white p-5 shadow-sm space-y-4">
            <div>
              <h2 className="font-medium">Before we backtest: missing parameters</h2>
              <p className="text-sm text-zinc-600">
                The source left these open. Required ones decide whether the strategy can be tested at all; the rest
                decide when it should <em>not</em> trade.
              </p>
            </div>
            <div className="grid gap-3 md:grid-cols-2">
              {questions.map((q) => (
                <label key={q.id} className="block rounded-lg border p-3 text-sm">
                  <span className="flex items-start gap-2">
                    {q.required && (
                      <span className="rounded bg-rose-100 px-1.5 text-xs font-medium text-rose-900">Required</span>
                    )}
                    <span className="font-medium">{q.question}</span>
                  </span>
                  <span className="mt-1 block text-xs text-zinc-500">{q.why}</span>
                  {q.options.length > 0 && (
                    <span className="mt-2 flex flex-wrap gap-1">
                      {q.options.map((o) => (
                        <button
                          key={o}
                          type="button"
                          onClick={() => setAnswers((a) => ({ ...a, [q.id]: o }))}
                          className={`rounded-full px-2 py-0.5 text-xs ${
                            answers[q.id] === o ? "bg-indigo-600 text-white" : "border"
                          }`}
                        >
                          {o}
                        </button>
                      ))}
                    </span>
                  )}
                  <input
                    className="mt-2 w-full rounded-md border px-2 py-1"
                    placeholder="Your answer"
                    value={answers[q.id] ?? ""}
                    onChange={(e) => setAnswers((a) => ({ ...a, [q.id]: e.target.value }))}
                  />
                </label>
              ))}
            </div>
            <button
              className="rounded-md bg-zinc-900 px-4 py-2 text-white disabled:opacity-50"
              disabled={loading || !Object.values(answers).some((v) => v.trim())}
              onClick={submitAnswers}
            >
              Re-extract with my answers
            </button>
          </section>
        )}

        {error && (
          <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-rose-900">{error}</div>
        )}

        {report && (
          <section className="rounded-xl border bg-white p-5 shadow-sm space-y-4">
            <div className="flex items-center justify-between gap-4 flex-wrap">
              <h2 className="font-medium">3. Profitability report</h2>
              <span className={`rounded-full px-3 py-1 text-sm font-medium ${verdictClass}`}>{verdict}</span>
            </div>
            <p className="text-sm text-zinc-600">{report.report?.narrative}</p>
            <div className="overflow-x-auto">
              <table className="min-w-full text-sm">
                <thead>
                  <tr className="border-b text-left">
                    <th className="py-2 pr-4">Scenario</th>
                    <th className="py-2 pr-4">Backtest trades</th>
                    <th className="py-2 pr-4">Backtest gross / net</th>
                    <th className="py-2 pr-4">Forward trades</th>
                    <th className="py-2 pr-4">Forward gross / net</th>
                    <th className="py-2 pr-4">Net per trade</th>
                    <th className="py-2 pr-4">Edge before costs</th>
                    <th className="py-2 pr-4">Enough trades</th>
                    <th className="py-2 pr-4">Pass</th>
                  </tr>
                </thead>
                <tbody>
                  {(report.report?.scenario_results ?? []).map((row, idx) => {
                    const trades = (row.backtest?.trades ?? 0) + (row.forward?.trades ?? 0);
                    const net = (row.backtest?.net_pnl ?? 0) + (row.forward?.net_pnl ?? 0);
                    return (
                      <tr key={idx} className="border-b">
                        <td className="py-2 pr-4 font-mono text-xs">#{idx + 1}</td>
                        <td className="py-2 pr-4">{row.backtest?.trades}</td>
                        <td className="py-2 pr-4">
                          {inr(row.backtest?.gross_pnl)} / {inr(row.backtest?.net_pnl)}
                        </td>
                        <td className="py-2 pr-4">{row.forward?.trades}</td>
                        <td className="py-2 pr-4">
                          {inr(row.forward?.gross_pnl)} / {inr(row.forward?.net_pnl)}
                        </td>
                        <td className="py-2 pr-4">{trades ? inr(net / trades) : "—"}</td>
                        <td className="py-2 pr-4">{row.edge_before_costs ? "Yes" : "No"}</td>
                        <td className="py-2 pr-4">{row.too_few_trades ? "No" : "Yes"}</td>
                        <td className="py-2 pr-4">{row.pass_net ? "Yes" : "No"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <SkippedSetups results={report.report?.scenario_results ?? []} />
            {report.report?.hard_stop_sweep && <HardStopTable sweep={report.report.hard_stop_sweep} />}
          </section>
        )}
      </div>
    </div>
  );
}
