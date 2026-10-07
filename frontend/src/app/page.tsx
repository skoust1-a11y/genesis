"use client";

import { useMemo, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8841";

type ReportPayload = {
  strategy_id: string;
  markdown?: string;
  report: {
    verdict?: { label?: string; scenarios_passing?: number; scenarios_tested?: number };
    narrative?: string;
    scenario_results?: Array<{
      pass_net?: boolean;
      backtest?: { net_pnl?: number; gross_pnl?: number; total_costs?: number; trades?: number };
      forward?: { net_pnl?: number; gross_pnl?: number; total_costs?: number; trades?: number };
    }>;
    cost_summary?: Record<string, unknown>;
  };
};

export default function Home() {
  const [sourceType, setSourceType] = useState("youtube");
  const [url, setUrl] = useState("");
  const [notes, setNotes] = useState("");
  const [markdown, setMarkdown] = useState("");
  const [strategyId, setStrategyId] = useState<string | null>(null);
  const [report, setReport] = useState<ReportPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const verdict = report?.report?.verdict?.label ?? "—";

  const verdictClass = useMemo(() => {
    if (verdict === "Profitable") return "bg-emerald-100 text-emerald-900";
    if (verdict === "Conditional") return "bg-amber-100 text-amber-900";
    if (verdict === "Not profitable") return "bg-rose-100 text-rose-900";
    return "bg-zinc-100 text-zinc-800";
  }, [verdict]);

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
      setStrategyId(data.strategy_id);
      setMarkdown(data.markdown);
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
          <h1 className="text-3xl font-semibold tracking-tight">Trading Strategy Lab</h1>
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
              disabled={loading || !markdown.trim()}
              onClick={runReview}
            >
              Run Review Crew
            </button>
          </div>
        </section>

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
                    <th className="py-2 pr-4">Backtest net</th>
                    <th className="py-2 pr-4">Forward net</th>
                    <th className="py-2 pr-4">Pass</th>
                  </tr>
                </thead>
                <tbody>
                  {(report.report?.scenario_results ?? []).map((row, idx) => (
                    <tr key={idx} className="border-b">
                      <td className="py-2 pr-4 font-mono text-xs">#{idx + 1}</td>
                      <td className="py-2 pr-4">{row.backtest?.net_pnl?.toFixed(0)}</td>
                      <td className="py-2 pr-4">{row.forward?.net_pnl?.toFixed(0)}</td>
                      <td className="py-2 pr-4">{row.pass_net ? "Yes" : "No"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}
      </div>
    </div>
  );
}
