"use client";

import { useEffect, useMemo, useState } from "react";
import { api, inr, pct } from "@/lib/api";

type StrategyCatalogItem = {
  id: string;
  slug: string;
  name: string;
  category: string;
  source: string;
  source_url?: string;
  primary_instrument: string;
  timeframe: string;
  status: string;
  verdict_badge?: string;
  current_version: number;
  win_rate?: number | null;
  net_pnl_inr?: number | null;
  profit_factor?: number | null;
  max_drawdown_inr?: number | null;
  trades_count?: number | null;
  risk_reward?: string;
  thesis: string;
  key_rules?: {
    entry?: string;
    exit?: string;
    risk?: string;
  };
  tested_findings?: string[];
  markdown?: string;
  has_rules?: boolean;
  gaps?: string[];
  created_at?: string | null;
  latest_report_id?: string | null;
};

type ForensicWeakness = {
  category: string;
  severity: "CRITICAL" | "HIGH" | "MEDIUM" | "LOW";
  title: string;
  detail: string;
  recommendation: string;
};

type OptimizationProposal = {
  id: string;
  parameter: string;
  current: string;
  proposed: string;
  impact: string;
  rationale: string;
};

type AnalysisResult = {
  status: string;
  baseline_version: string;
  strategy_name: string;
  reconstructed: {
    hypothesis: string;
    instrument: string;
    timeframe: string;
    entry_conditions: string[];
    exit_conditions: string[];
    risk_management: string[];
    identified_gaps: string[];
  };
  forensic_weaknesses: ForensicWeakness[];
  optimization_proposals: OptimizationProposal[];
  ai_audit_narrative?: string | null;
  improved_markdown?: string | null;
};

type HardStopRow = {
  stop_pct: number;
  stop_inr: number;
  target_inr: number;
  backtest: { trades: number; win_rate: number; net_pnl: number };
  forward: { trades: number; win_rate: number; net_pnl: number };
  worst_loss: number | null;
  max_drawdown: number;
};

type HardStopSweep = {
  capital_inr: number;
  risk_reward: number | null;
  rows: HardStopRow[];
  recommended_pct: number | null;
  reason: string;
};

type ReviewReport = {
  report_id: string;
  strategy_id: string;
  strategy_name?: string;
  report: {
    verdict?: {
      label?: string;
      scenarios_passing?: number;
      scenarios_tested?: number;
      narrative?: string;
    };
    narrative?: string;
    hard_stop_sweep?: HardStopSweep;
    scenario_results?: Array<{
      pass_net?: boolean;
      edge_before_costs?: boolean;
      backtest?: { trades?: number; win_rate?: number; net_pnl?: number };
      forward?: { trades?: number; win_rate?: number; net_pnl?: number };
    }>;
  };
};

export default function StrategiesPage() {
  const [catalog, setCatalog] = useState<StrategyCatalogItem[]>([]);
  const [loadingCatalog, setLoadingCatalog] = useState(true);
  const [selectedStrategy, setSelectedStrategy] = useState<StrategyCatalogItem | null>(null);
  const [activeTab, setActiveTab] = useState<"catalog" | "analyzer">("catalog");

  // Search & Filters
  const [statusFilter, setStatusFilter] = useState("all");
  const [searchQuery, setSearchQuery] = useState("");

  // Analyzer State
  const [analyzerStrategyId, setAnalyzerStrategyId] = useState<string>("");
  const [strategyInputText, setStrategyInputText] = useState("");
  const [analyzing, setAnalyzing] = useState(false);
  const [analysisResult, setAnalysisResult] = useState<AnalysisResult | null>(null);
  const [analyzerError, setAnalyzerError] = useState<string | null>(null);

  // Review & Simulation state
  const [runningReview, setRunningReview] = useState(false);
  const [reviewReport, setReviewReport] = useState<ReviewReport | null>(null);
  const [reviewError, setReviewError] = useState<string | null>(null);

  // Load catalog on mount
  useEffect(() => {
    async function loadCatalog() {
      setLoadingCatalog(true);
      try {
        const res = await api<{ catalog: StrategyCatalogItem[] }>("/api/strategies/catalog");
        setCatalog(res.catalog || []);
        if (res.catalog?.length > 0) {
          setSelectedStrategy(res.catalog[0]);
          setAnalyzerStrategyId(res.catalog[0].id);
          setStrategyInputText(res.catalog[0].markdown || "");
        }
      } catch (err) {
        console.error("Failed to load strategy catalog:", err);
      } finally {
        setLoadingCatalog(false);
      }
    }
    loadCatalog();
  }, []);

  // Filtered catalog
  const filteredCatalog = useMemo(() => {
    return catalog.filter((item) => {
      const matchesStatus =
        statusFilter === "all" ||
        item.status.toLowerCase().includes(statusFilter.toLowerCase()) ||
        (item.verdict_badge && item.verdict_badge.toLowerCase().includes(statusFilter.toLowerCase()));
      const matchesQuery =
        !searchQuery ||
        item.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
        item.thesis.toLowerCase().includes(searchQuery.toLowerCase()) ||
        item.category.toLowerCase().includes(searchQuery.toLowerCase()) ||
        item.primary_instrument.toLowerCase().includes(searchQuery.toLowerCase());
      return matchesStatus && matchesQuery;
    });
  }, [catalog, statusFilter, searchQuery]);

  // Catalog aggregated statistics
  const catalogStats = useMemo(() => {
    const total = catalog.length;
    const tested = catalog.filter((c) => c.status === "Tested" || c.status === "Forward-Tested").length;
    const totalTrades = catalog.reduce((acc, c) => acc + (c.trades_count || 0), 0);
    const bestPf = Math.max(...catalog.map((c) => c.profit_factor || 0), 1.0);
    return { total, tested, totalTrades, bestPf };
  }, [catalog]);

  function handleSelectForAnalysis(strat: StrategyCatalogItem) {
    setSelectedStrategy(strat);
    setAnalyzerStrategyId(strat.id);
    setStrategyInputText(strat.markdown || "");
    setActiveTab("analyzer");
    setAnalysisResult(null);
    setReviewReport(null);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function runAnalyzer() {
    if (!strategyInputText.trim() && !analyzerStrategyId) {
      setAnalyzerError("Please select a strategy or paste your strategy rules.");
      return;
    }
    setAnalyzing(true);
    setAnalyzerError(null);
    try {
      const res = await api<AnalysisResult>("/api/strategies/analyze", {
        body: {
          strategy_id: analyzerStrategyId || undefined,
          text: strategyInputText || undefined,
        },
      });
      setAnalysisResult(res);
    } catch (err) {
      setAnalyzerError(err instanceof Error ? err.message : "Strategy analysis failed");
    } finally {
      setAnalyzing(false);
    }
  }

  async function executeReview(markdownToTest?: string) {
    setRunningReview(true);
    setReviewError(null);
    try {
      const res = await api<ReviewReport>("/api/review/run", {
        body: {
          strategy_id: analyzerStrategyId || undefined,
          markdown: markdownToTest || strategyInputText || undefined,
        },
      });
      setReviewReport(res);
    } catch (err) {
      setReviewError(err instanceof Error ? err.message : "Review simulation failed");
    } finally {
      setRunningReview(false);
    }
  }

  return (
    <div className="min-h-screen bg-zinc-50 text-zinc-900 pb-16">
      <div className="mx-auto max-w-7xl px-4 py-8 space-y-6">
        {/* Header Block matching Lab / Markets / Dashboard */}
        <header className="space-y-2">
          <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
            <div>
              <div className="flex items-center gap-2">
                <span className="inline-block h-2.5 w-2.5 rounded-full bg-emerald-600" />
                <span className="text-xs font-semibold uppercase tracking-wider text-emerald-800">
                  Institutional Research & Audit
                </span>
              </div>
              <h1 className="text-3xl font-semibold tracking-tight text-zinc-900 mt-1">
                Strategies & Strategy Analyzer
              </h1>
              <p className="text-sm text-zinc-600 max-w-3xl mt-1">
                Browse tested and researched institutional strategies, review empirical backtest results with Indian options charges,
                and run the Strategy Analyzer to forensic-audit risks and optimize edge before live deployment.
              </p>
            </div>

            {/* Tab Pills */}
            <div className="inline-flex rounded-lg border bg-white p-1 shadow-sm self-start">
              <button
                onClick={() => setActiveTab("catalog")}
                className={`rounded-md px-4 py-1.5 text-sm font-medium transition-all ${
                  activeTab === "catalog"
                    ? "bg-zinc-900 text-white shadow"
                    : "text-zinc-600 hover:text-zinc-900 hover:bg-zinc-50"
                }`}
              >
                Strategy Library ({catalog.length})
              </button>
              <button
                onClick={() => setActiveTab("analyzer")}
                className={`rounded-md px-4 py-1.5 text-sm font-medium transition-all flex items-center gap-1.5 ${
                  activeTab === "analyzer"
                    ? "bg-zinc-900 text-white shadow"
                    : "text-zinc-600 hover:text-zinc-900 hover:bg-zinc-50"
                }`}
              >
                <span>Strategy Analyzer</span>
                <span className="rounded bg-emerald-100 text-emerald-800 text-[10px] font-bold px-1.5 py-0.5">
                  AI + Quant
                </span>
              </button>
            </div>
          </div>
        </header>

        {/* Stats Strip matching Dashboard */}
        <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <div className="rounded-xl border bg-white p-4 shadow-sm">
            <div className="text-xs text-zinc-500 font-medium">Catalog Strategies</div>
            <div className="text-2xl font-semibold text-zinc-900 mt-1">{catalogStats.total} Systems</div>
            <div className="text-xs text-zinc-500 mt-0.5">Machine executable & research</div>
          </div>

          <div className="rounded-xl border bg-white p-4 shadow-sm">
            <div className="text-xs text-zinc-500 font-medium">Empirically Tested</div>
            <div className="text-2xl font-semibold text-emerald-700 mt-1">{catalogStats.tested} Validated</div>
            <div className="text-xs text-zinc-500 mt-0.5">Tested on 2020-2026 NSE data</div>
          </div>

          <div className="rounded-xl border bg-white p-4 shadow-sm">
            <div className="text-xs text-zinc-500 font-medium">Verified Historical Trades</div>
            <div className="text-2xl font-semibold text-zinc-900 mt-1">{catalogStats.totalTrades}+ Fills</div>
            <div className="text-xs text-zinc-500 mt-0.5">Real spreads, STT, and slippage</div>
          </div>

          <div className="rounded-xl border bg-white p-4 shadow-sm">
            <div className="text-xs text-zinc-500 font-medium">Peak Profit Factor</div>
            <div className="text-2xl font-semibold text-amber-700 mt-1">{catalogStats.bestPf.toFixed(2)}x</div>
            <div className="text-xs text-zinc-500 mt-0.5">Range sweeps & breakout systems</div>
          </div>
        </section>

        {/* ================= TAB 1: STRATEGY CATALOG ================= */}
        {activeTab === "catalog" && (
          <div className="space-y-6">
            {/* Filter and Search Bar */}
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 rounded-xl border bg-white p-4 shadow-sm">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-medium text-zinc-500 uppercase mr-1">Filter:</span>
                {["all", "tested", "forward-tested", "research"].map((s) => (
                  <button
                    key={s}
                    onClick={() => setStatusFilter(s)}
                    className={`rounded-md px-3 py-1 text-xs font-medium capitalize transition-colors ${
                      statusFilter === s
                        ? "bg-zinc-900 text-white"
                        : "bg-zinc-100 text-zinc-600 hover:bg-zinc-200 hover:text-zinc-900"
                    }`}
                  >
                    {s}
                  </button>
                ))}
              </div>

              <div className="w-full sm:w-72">
                <input
                  type="text"
                  placeholder="Search by name, instrument, rule..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="w-full rounded-md border border-zinc-300 bg-white px-3 py-1.5 text-sm text-zinc-900 placeholder:text-zinc-400 focus:outline-none focus:ring-1 focus:ring-emerald-600"
                />
              </div>
            </div>

            {/* Catalog Grid */}
            {loadingCatalog ? (
              <div className="py-20 text-center text-sm text-zinc-500">Loading strategy catalog...</div>
            ) : filteredCatalog.length === 0 ? (
              <div className="rounded-xl border bg-white p-12 text-center shadow-sm">
                <p className="text-sm font-medium text-zinc-600">No strategies found matching your search.</p>
                <button
                  onClick={() => {
                    setStatusFilter("all");
                    setSearchQuery("");
                  }}
                  className="mt-2 text-xs font-medium text-emerald-700 hover:underline"
                >
                  Reset filters
                </button>
              </div>
            ) : (
              <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3">
                {filteredCatalog.map((strat) => {
                  const isTested = strat.status === "Tested" || strat.status === "Forward-Tested";
                  const isSelected = selectedStrategy?.slug === strat.slug;

                  return (
                    <div
                      key={strat.id}
                      className={`flex flex-col justify-between rounded-xl border bg-white p-5 shadow-sm transition-all hover:shadow-md ${
                        isSelected ? "border-emerald-600 ring-1 ring-emerald-600" : "border-zinc-200"
                      }`}
                    >
                      <div className="space-y-3.5">
                        {/* Header Badges */}
                        <div className="flex items-start justify-between gap-2">
                          <span className="text-xs font-semibold text-zinc-500 uppercase tracking-wide">
                            {strat.category}
                          </span>
                          <span
                            className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${
                              strat.status === "Forward-Tested"
                                ? "bg-emerald-100 text-emerald-900"
                                : isTested
                                ? "bg-blue-100 text-blue-900"
                                : "bg-amber-100 text-amber-900"
                            }`}
                          >
                            {strat.verdict_badge || strat.status}
                          </span>
                        </div>

                        {/* Title & Instrument */}
                        <div>
                          <h3 className="text-base font-semibold text-zinc-900">{strat.name}</h3>
                          <div className="flex items-center gap-2 mt-1 text-xs text-zinc-500">
                            <span className="font-mono font-medium text-emerald-700">{strat.primary_instrument}</span>
                            <span>•</span>
                            <span>{strat.timeframe}</span>
                          </div>
                        </div>

                        {/* Metrics Strip */}
                        {isTested && strat.win_rate != null ? (
                          <div className="grid grid-cols-3 gap-2 rounded-lg border bg-zinc-50 p-2.5 text-center">
                            <div>
                              <div className="text-[10px] uppercase font-medium text-zinc-500">Win Rate</div>
                              <div className="text-sm font-semibold text-zinc-900 mt-0.5">
                                {pct(strat.win_rate * 100, 1)}
                              </div>
                            </div>
                            <div>
                              <div className="text-[10px] uppercase font-medium text-zinc-500">Net PnL</div>
                              <div
                                className={`text-sm font-semibold mt-0.5 ${
                                  (strat.net_pnl_inr || 0) >= 0 ? "text-emerald-700" : "text-rose-700"
                                }`}
                              >
                                {inr(strat.net_pnl_inr)}
                              </div>
                            </div>
                            <div>
                              <div className="text-[10px] uppercase font-medium text-zinc-500">Profit Factor</div>
                              <div className="text-sm font-semibold text-amber-800 mt-0.5">
                                {strat.profit_factor ? `${strat.profit_factor.toFixed(2)}x` : "—"}
                              </div>
                            </div>
                          </div>
                        ) : (
                          <div className="rounded-lg border bg-zinc-50 p-2.5 text-xs text-zinc-600 flex items-center justify-between">
                            <span>Target Payoff: <strong className="text-zinc-900">{strat.risk_reward || "1:2.0"}</strong></span>
                            <span className="rounded bg-amber-100 px-2 py-0.5 font-medium text-amber-900 text-[11px]">
                              Spec Ready
                            </span>
                          </div>
                        )}

                        {/* Thesis */}
                        <p className="text-xs text-zinc-600 line-clamp-3 leading-relaxed">{strat.thesis}</p>

                        {/* Empirical Takeaway */}
                        {strat.tested_findings && strat.tested_findings.length > 0 && (
                          <div className="pt-2 border-t border-zinc-100">
                            <div className="text-[11px] font-medium text-zinc-700 bg-zinc-50 border rounded-md p-2">
                              <span className="font-semibold text-emerald-800 mr-1">Key finding:</span>
                              {strat.tested_findings[0]}
                            </div>
                          </div>
                        )}
                      </div>

                      {/* Card Action Footer */}
                      <div className="pt-4 mt-3 border-t border-zinc-100 flex items-center justify-between gap-3">
                        <button
                          onClick={() => setSelectedStrategy(strat)}
                          className="text-xs text-zinc-600 hover:text-zinc-900 font-medium"
                        >
                          View Details
                        </button>
                        <button
                          onClick={() => handleSelectForAnalysis(strat)}
                          className="rounded-md bg-emerald-600 hover:bg-emerald-700 text-white px-3 py-1.5 text-xs font-medium shadow-sm transition-colors flex items-center gap-1"
                        >
                          <span>Analyze & Improve</span>
                          <span>→</span>
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}

            {/* Selected Strategy Detail Section */}
            {selectedStrategy && (
              <div className="rounded-xl border bg-white p-6 shadow-sm space-y-6">
                <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b pb-4">
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="text-xs font-semibold uppercase text-emerald-700">
                        {selectedStrategy.category}
                      </span>
                      <span className="text-zinc-400">•</span>
                      <span className="text-xs text-zinc-500">Version {selectedStrategy.current_version}</span>
                    </div>
                    <h2 className="text-xl font-semibold text-zinc-900 mt-1">{selectedStrategy.name}</h2>
                    {selectedStrategy.source_url && (
                      <a
                        href={selectedStrategy.source_url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-xs text-emerald-700 hover:underline mt-0.5 inline-block"
                      >
                        Source: {selectedStrategy.source}
                      </a>
                    )}
                  </div>

                  <button
                    onClick={() => handleSelectForAnalysis(selectedStrategy)}
                    className="rounded-md bg-emerald-600 hover:bg-emerald-700 text-white px-4 py-2 text-sm font-medium shadow-sm transition-colors self-start"
                  >
                    Load in Strategy Analyzer →
                  </button>
                </div>

                <div className="grid md:grid-cols-3 gap-6">
                  <div className="space-y-4 md:col-span-2">
                    <div>
                      <h4 className="text-xs font-semibold uppercase tracking-wider text-zinc-500">Thesis & Concept</h4>
                      <p className="mt-1 text-sm text-zinc-700 leading-relaxed bg-zinc-50 p-3.5 rounded-lg border">
                        {selectedStrategy.thesis}
                      </p>
                    </div>

                    <div className="grid sm:grid-cols-3 gap-3">
                      <div className="bg-zinc-50 p-3 rounded-lg border">
                        <div className="text-xs font-semibold text-emerald-800 uppercase">Entry Setup</div>
                        <div className="text-xs text-zinc-700 mt-1">
                          {selectedStrategy.key_rules?.entry || "See rules"}
                        </div>
                      </div>
                      <div className="bg-zinc-50 p-3 rounded-lg border">
                        <div className="text-xs font-semibold text-blue-800 uppercase">Exit & Target</div>
                        <div className="text-xs text-zinc-700 mt-1">
                          {selectedStrategy.key_rules?.exit || "See rules"}
                        </div>
                      </div>
                      <div className="bg-zinc-50 p-3 rounded-lg border">
                        <div className="text-xs font-semibold text-amber-800 uppercase">Risk Management</div>
                        <div className="text-xs text-zinc-700 mt-1">
                          {selectedStrategy.key_rules?.risk || "See rules"}
                        </div>
                      </div>
                    </div>

                    {selectedStrategy.tested_findings && selectedStrategy.tested_findings.length > 0 && (
                      <div>
                        <h4 className="text-xs font-semibold uppercase tracking-wider text-zinc-500">
                          Empirical Audit & Backtest Learnings
                        </h4>
                        <ul className="mt-2 space-y-2">
                          {selectedStrategy.tested_findings.map((f, i) => (
                            <li
                              key={i}
                              className="text-xs text-zinc-700 bg-zinc-50 p-2.5 rounded-md border flex items-start gap-2"
                            >
                              <span className="text-emerald-700 font-bold">✓</span>
                              <span>{f}</span>
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                  </div>

                  {/* Sidebar Metadata & Markdown */}
                  <div className="space-y-4">
                    <div className="bg-zinc-50 p-4 rounded-lg border space-y-2 text-xs">
                      <h4 className="font-semibold text-zinc-700 uppercase tracking-wider text-[11px]">System Parameters</h4>
                      <div className="flex justify-between py-1 border-b">
                        <span className="text-zinc-500">Instrument</span>
                        <span className="font-mono text-zinc-800">{selectedStrategy.primary_instrument}</span>
                      </div>
                      <div className="flex justify-between py-1 border-b">
                        <span className="text-zinc-500">Timeframe</span>
                        <span className="font-mono text-zinc-800">{selectedStrategy.timeframe}</span>
                      </div>
                      <div className="flex justify-between py-1 border-b">
                        <span className="text-zinc-500">Target Payoff</span>
                        <span className="font-mono text-zinc-800">{selectedStrategy.risk_reward || "1:2.0"}</span>
                      </div>
                      <div className="flex justify-between py-1">
                        <span className="text-zinc-500">Tested Trades</span>
                        <span className="font-mono text-zinc-800">{selectedStrategy.trades_count || "—"}</span>
                      </div>
                    </div>

                    {selectedStrategy.markdown && (
                      <div className="bg-zinc-50 p-3.5 rounded-lg border space-y-1.5">
                        <div className="flex items-center justify-between">
                          <h4 className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
                            Strategy Specification
                          </h4>
                          <span className="text-[10px] text-zinc-500 font-mono">YAML + Markdown</span>
                        </div>
                        <pre className="text-[11px] font-mono text-zinc-700 bg-white p-3 rounded border overflow-x-auto max-h-48 scrollbar-thin">
                          {selectedStrategy.markdown}
                        </pre>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            )}
          </div>
        )}

        {/* ================= TAB 2: INSTITUTIONAL STRATEGY ANALYZER ================= */}
        {activeTab === "analyzer" && (
          <div className="space-y-6">
            {/* Input & Baseline Setup */}
            <div className="rounded-xl border bg-white p-6 shadow-sm space-y-5">
              <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b pb-4">
                <div>
                  <h2 className="text-lg font-semibold text-zinc-900">Step 1 — Strategy Ingestion & Baseline (V0)</h2>
                  <p className="text-xs text-zinc-600 mt-0.5">
                    Select a strategy from the catalog or paste your trading rules. The analyzer locks it as Version 0 (V0)
                    to benchmark forensic improvements.
                  </p>
                </div>

                <div className="flex items-center gap-2">
                  <label className="text-xs text-zinc-500 font-medium whitespace-nowrap">Load Strategy:</label>
                  <select
                    value={analyzerStrategyId}
                    onChange={(e) => {
                      const id = e.target.value;
                      setAnalyzerStrategyId(id);
                      const found = catalog.find((c) => c.id === id);
                      if (found && found.markdown) setStrategyInputText(found.markdown);
                    }}
                    className="rounded-md border border-zinc-300 bg-white px-3 py-1.5 text-xs text-zinc-900 focus:outline-none focus:ring-1 focus:ring-emerald-600"
                  >
                    <option value="">-- Choose from Catalog --</option>
                    {catalog.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name} ({c.primary_instrument})
                      </option>
                    ))}
                  </select>
                </div>
              </div>

              {/* Textarea Input */}
              <div className="space-y-1.5">
                <div className="flex items-center justify-between text-xs text-zinc-600">
                  <span className="font-medium">Strategy Rules & Dimensions:</span>
                  <span className="text-zinc-500">Concept, Instrument, Timeframe, Entry, Exit, Stops & Risk</span>
                </div>
                <textarea
                  rows={8}
                  value={strategyInputText}
                  onChange={(e) => setStrategyInputText(e.target.value)}
                  placeholder="Paste complete strategy: Concept, Instrument (e.g. NIFTY Weekly Options), Entry rules, Exit rules, Stop-loss placement, Indicators, Position sizing, and Restrictions..."
                  className="w-full rounded-md border border-zinc-300 bg-white p-3 font-mono text-xs text-zinc-900 placeholder:text-zinc-400 focus:outline-none focus:ring-1 focus:ring-emerald-600"
                />
              </div>

              {/* Quick Dimension Insert Buttons */}
              <div className="flex flex-wrap items-center gap-2 pt-1">
                <span className="text-xs text-zinc-500 font-medium">Add Template:</span>
                {[
                  { label: "+ Concept / Thesis", text: "\n# Thesis\nCapture liquidity sweep beyond swing highs/lows with reversal confirmation." },
                  { label: "+ Entry Conditions", text: "\n## Entry\n- 5-min candle sweeps PDH/PDL and closes back inside\n- Enter on confirmation candle break" },
                  { label: "+ Exit & Stop-Loss", text: "\n## Exit\n- Stop-loss 0.1% behind sweep wick\n- Target next opposing liquidity pool (1:2.5 R:R)\n- Square-off by 3:15 PM" },
                  { label: "+ Risk Management", text: "\n## Risk\n- Max 1 lot fixed\n- Max daily loss 2%\n- 3-day cooldown on 3 consecutive losses" },
                ].map((item) => (
                  <button
                    key={item.label}
                    onClick={() => setStrategyInputText((prev) => prev + item.text)}
                    className="rounded-md border bg-zinc-50 px-2.5 py-1 text-xs text-zinc-700 hover:bg-zinc-100 transition-colors"
                  >
                    {item.label}
                  </button>
                ))}
              </div>

              {/* Action Button */}
              <div className="flex items-center justify-between pt-3 border-t">
                <span className="text-xs text-zinc-500">
                  Analyzer will identify gaps, audit friction, and propose quantitative improvements.
                </span>

                <button
                  disabled={analyzing}
                  onClick={runAnalyzer}
                  className="rounded-md bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 text-white px-5 py-2 text-sm font-medium shadow-sm transition-colors flex items-center gap-2"
                >
                  {analyzing ? (
                    <>
                      <span className="h-4 w-4 rounded-full border-2 border-white/30 border-t-white animate-spin" />
                      <span>Auditing Strategy...</span>
                    </>
                  ) : (
                    <>
                      <span>Run Strategy Analyzer</span>
                      <span>⚡</span>
                    </>
                  )}
                </button>
              </div>

              {analyzerError && (
                <div className="rounded-md border border-rose-200 bg-rose-50 p-3 text-xs text-rose-900">
                  {analyzerError}
                </div>
              )}
            </div>

            {/* Analysis Output Section */}
            {analysisResult && (
              <div className="space-y-6">
                {/* Baseline Freeze Banner */}
                <div className="rounded-xl border border-emerald-200 bg-emerald-50/80 p-4 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                  <div className="flex items-center gap-3">
                    <span className="flex h-7 w-7 items-center justify-center rounded-md bg-emerald-600 text-white font-semibold text-xs">
                      V0
                    </span>
                    <div>
                      <div className="text-xs font-semibold uppercase text-emerald-800">Baseline Locked</div>
                      <div className="text-sm font-semibold text-zinc-900">
                        {analysisResult.strategy_name} — Frozen as Benchmark V0
                      </div>
                    </div>
                  </div>
                  <span className="text-xs text-zinc-600 bg-white border rounded px-3 py-1">
                    Benchmark for all forensic audit & optimization steps
                  </span>
                </div>

                {/* Stage 1: Quantitative Rule Reconstitution & Gap Detection */}
                <div className="rounded-xl border bg-white p-6 shadow-sm space-y-4">
                  <div className="border-b pb-3">
                    <span className="text-xs font-semibold uppercase text-emerald-800">Stage 1</span>
                    <h3 className="text-base font-semibold text-zinc-900">Quantitative Rule Reconstitution</h3>
                    <p className="text-xs text-zinc-500">Trading rules reconstructed into machine-verifiable criteria</p>
                  </div>

                  <div className="grid md:grid-cols-2 gap-6">
                    <div className="space-y-3 bg-zinc-50 p-4 rounded-lg border text-xs">
                      <div>
                        <span className="font-semibold text-zinc-600">Instrument: </span>
                        <span className="text-zinc-900">{analysisResult.reconstructed.instrument}</span>
                      </div>
                      <div>
                        <span className="font-semibold text-zinc-600">Timeframe: </span>
                        <span className="text-zinc-900">{analysisResult.reconstructed.timeframe}</span>
                      </div>
                      <div className="pt-2 border-t">
                        <div className="font-semibold text-zinc-600 mb-1">Entry Conditions:</div>
                        <ul className="space-y-1 pl-4 list-disc text-zinc-700">
                          {analysisResult.reconstructed.entry_conditions.map((c, i) => (
                            <li key={i}>{c}</li>
                          ))}
                        </ul>
                      </div>
                      <div className="pt-2 border-t">
                        <div className="font-semibold text-zinc-600 mb-1">Exit Rules:</div>
                        <ul className="space-y-1 pl-4 list-disc text-zinc-700">
                          {analysisResult.reconstructed.exit_conditions.map((c, i) => (
                            <li key={i}>{c}</li>
                          ))}
                        </ul>
                      </div>
                      <div className="pt-2 border-t">
                        <div className="font-semibold text-zinc-600 mb-1">Risk Management:</div>
                        <ul className="space-y-1 pl-4 list-disc text-zinc-700">
                          {analysisResult.reconstructed.risk_management.map((c, i) => (
                            <li key={i}>{c}</li>
                          ))}
                        </ul>
                      </div>
                    </div>

                    {/* Identified Gaps */}
                    <div className="bg-zinc-50 p-4 rounded-lg border space-y-3">
                      <div className="flex items-center gap-2">
                        <span className="h-2 w-2 rounded-full bg-amber-500" />
                        <h4 className="text-xs font-semibold uppercase text-amber-900">
                          Identified Gaps & Unquantified Rules
                        </h4>
                      </div>
                      <p className="text-xs text-zinc-600">
                        Institutional systems require strict quantitative definition. The following rules were missing or ambiguous:
                      </p>
                      <ul className="space-y-2">
                        {analysisResult.reconstructed.identified_gaps.map((gap, i) => (
                          <li
                            key={i}
                            className="text-xs text-amber-900 bg-amber-50 border border-amber-200 p-2.5 rounded flex items-start gap-2"
                          >
                            <span className="text-amber-700 font-bold">!</span>
                            <span>{gap}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  </div>
                </div>

                {/* Stage 2: Forensic Weakness Audit */}
                <div className="rounded-xl border bg-white p-6 shadow-sm space-y-4">
                  <div className="border-b pb-3">
                    <span className="text-xs font-semibold uppercase text-rose-800">Stage 2</span>
                    <h3 className="text-base font-semibold text-zinc-900">Forensic Execution & Market Friction Audit</h3>
                    <p className="text-xs text-zinc-500">Real-world institutional risks, Indian options costs, and Greek drag</p>
                  </div>

                  <div className="grid gap-4 sm:grid-cols-2">
                    {analysisResult.forensic_weaknesses.map((w, i) => (
                      <div key={i} className="rounded-lg border bg-zinc-50/60 p-4 space-y-2">
                        <div className="flex items-center justify-between">
                          <span className="text-[11px] font-semibold text-zinc-500 uppercase">{w.category}</span>
                          <span
                            className={`text-[10px] font-bold px-2 py-0.5 rounded ${
                              w.severity === "CRITICAL"
                                ? "bg-rose-100 text-rose-900 border border-rose-200"
                                : w.severity === "HIGH"
                                ? "bg-amber-100 text-amber-900 border border-amber-200"
                                : "bg-blue-100 text-blue-900 border border-blue-200"
                            }`}
                          >
                            {w.severity}
                          </span>
                        </div>
                        <h4 className="text-sm font-semibold text-zinc-900">{w.title}</h4>
                        <p className="text-xs text-zinc-600 leading-relaxed">{w.detail}</p>
                        <div className="text-xs text-emerald-900 bg-emerald-50 border border-emerald-200 p-2 rounded mt-2">
                          <strong>Institutional Fix:</strong> {w.recommendation}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>

                {/* Stage 3: Optimization Proposals Table */}
                <div className="rounded-xl border bg-white p-6 shadow-sm space-y-4">
                  <div className="border-b pb-3">
                    <span className="text-xs font-semibold uppercase text-emerald-800">Stage 3</span>
                    <h3 className="text-base font-semibold text-zinc-900">Recommended Quantitative Improvements</h3>
                    <p className="text-xs text-zinc-500">Concrete parameter adjustments to maximize edge and survival</p>
                  </div>

                  <div className="overflow-x-auto">
                    <table className="min-w-full text-left text-xs">
                      <thead>
                        <tr className="border-b text-zinc-500">
                          <th className="py-2.5 pr-4 font-semibold uppercase">Parameter Area</th>
                          <th className="py-2.5 pr-4 font-semibold uppercase">Current (V0)</th>
                          <th className="py-2.5 pr-4 font-semibold uppercase text-emerald-800">Proposed (V1)</th>
                          <th className="py-2.5 pr-4 font-semibold uppercase">Expected Impact</th>
                          <th className="py-2.5 pr-4 font-semibold uppercase">Institutional Rationale</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-zinc-100">
                        {analysisResult.optimization_proposals.map((p) => (
                          <tr key={p.id} className="hover:bg-zinc-50">
                            <td className="py-3 pr-4 font-semibold text-zinc-900">{p.parameter}</td>
                            <td className="py-3 pr-4 text-zinc-600 font-mono">{p.current}</td>
                            <td className="py-3 pr-4 text-emerald-800 font-mono font-bold bg-emerald-50/60 rounded">
                              {p.proposed}
                            </td>
                            <td className="py-3 pr-4 text-indigo-700 font-medium">{p.impact}</td>
                            <td className="py-3 pr-4 text-zinc-600">{p.rationale}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>

                {/* Stage 4: AI Quantitative Portfolio Manager Assessment */}
                {analysisResult.ai_audit_narrative && (
                  <div className="rounded-xl border bg-white p-6 shadow-sm space-y-3">
                    <div className="flex items-center gap-2 border-b pb-3">
                      <span className="h-2.5 w-2.5 rounded-full bg-emerald-600 animate-pulse" />
                      <h3 className="text-sm font-semibold text-zinc-900">
                        AI Quantitative Portfolio Manager Assessment
                      </h3>
                    </div>
                    <div className="text-xs text-zinc-800 leading-relaxed whitespace-pre-wrap font-mono bg-zinc-50 p-4 rounded-lg border max-h-96 overflow-y-auto">
                      {analysisResult.ai_audit_narrative}
                    </div>
                  </div>
                )}

                {/* Stage 5: Executable Candidate V1 & Review Simulator */}
                <div className="rounded-xl border border-emerald-200 bg-white p-6 shadow-sm space-y-5">
                  <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b pb-4">
                    <div>
                      <span className="text-xs font-semibold uppercase text-emerald-800">Stage 4</span>
                      <h3 className="text-lg font-semibold text-zinc-900">Executable Institutional Candidate (V1)</h3>
                      <p className="text-xs text-zinc-500 mt-0.5">
                        Synthesized into a machine-verifiable strategy specification ready for Review Crew backtest.
                      </p>
                    </div>

                    <button
                      disabled={runningReview}
                      onClick={() => executeReview(analysisResult.improved_markdown || undefined)}
                      className="rounded-md bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 text-white px-4 py-2 text-xs font-semibold shadow-sm transition-colors flex items-center gap-1.5 self-start"
                    >
                      {runningReview ? (
                        <>
                          <span className="h-3.5 w-3.5 rounded-full border-2 border-white/30 border-t-white animate-spin" />
                          <span>Simulating with Review Crew...</span>
                        </>
                      ) : (
                        <>
                          <span>Run Review Simulation on V1</span>
                          <span>▶</span>
                        </>
                      )}
                    </button>
                  </div>

                  {analysisResult.improved_markdown && (
                    <div className="space-y-1.5">
                      <div className="text-xs text-zinc-600 font-medium">V1 Strategy Markdown Specification:</div>
                      <pre className="text-xs font-mono text-zinc-800 bg-zinc-50 p-3.5 rounded-lg border max-h-60 overflow-y-auto overflow-x-auto scrollbar-thin">
                        {analysisResult.improved_markdown}
                      </pre>
                    </div>
                  )}

                  {reviewError && (
                    <div className="rounded-md border border-rose-200 bg-rose-50 p-3 text-xs text-rose-900">
                      {reviewError}
                    </div>
                  )}

                  {/* Review Simulation Results */}
                  {reviewReport && (
                    <div className="mt-4 pt-4 border-t space-y-4">
                      <div className="flex items-center justify-between">
                        <div>
                          <div className="text-xs uppercase font-semibold text-emerald-800">Review Crew Verdict</div>
                          <div className="text-xl font-bold text-zinc-900 mt-0.5">
                            {reviewReport.report?.verdict?.label || "Completed"}
                          </div>
                        </div>
                        <span className="text-xs font-mono text-zinc-500 bg-zinc-100 px-2.5 py-1 rounded">
                          Report ID: {reviewReport.report_id.slice(0, 8)}...
                        </span>
                      </div>

                      {reviewReport.report?.narrative && (
                        <p className="text-xs text-zinc-700 bg-zinc-50 p-3 rounded-lg border">
                          {reviewReport.report.narrative}
                        </p>
                      )}

                      {/* Hard Stop Loss Sweep Table */}
                      {reviewReport.report?.hard_stop_sweep && (
                        <div className="space-y-2">
                          <h4 className="text-xs font-semibold uppercase text-zinc-600">
                            Hard Stop-Loss Sweep Analysis (% of Capital)
                          </h4>
                          <div className="overflow-x-auto">
                            <table className="min-w-full text-xs text-left">
                              <thead>
                                <tr className="border-b text-zinc-500">
                                  <th className="py-2 pr-3">Stop %</th>
                                  <th className="py-2 pr-3">₹ stop / ₹ target</th>
                                  <th className="py-2 pr-3">Backtest Trades</th>
                                  <th className="py-2 pr-3">Win Rate</th>
                                  <th className="py-2 pr-3">Net PnL</th>
                                  <th className="py-2 pr-3">Forward Trades</th>
                                  <th className="py-2 pr-3">Forward Win%</th>
                                  <th className="py-2 pr-3">Forward Net</th>
                                  <th className="py-2 pr-3">Worst Loss</th>
                                </tr>
                              </thead>
                              <tbody className="divide-y divide-zinc-100">
                                {reviewReport.report.hard_stop_sweep.rows.map((row) => {
                                  const isRec = row.stop_pct === reviewReport.report.hard_stop_sweep?.recommended_pct;
                                  return (
                                    <tr
                                      key={row.stop_pct}
                                      className={`hover:bg-zinc-50 ${isRec ? "bg-emerald-50/70 font-semibold text-emerald-900" : ""}`}
                                    >
                                      <td className="py-2 pr-3">{row.stop_pct}%</td>
                                      <td className="py-2 pr-3 font-mono text-zinc-600">
                                        {inr(row.stop_inr)} / {inr(row.target_inr)}
                                      </td>
                                      <td className="py-2 pr-3 font-mono">{row.backtest.trades}</td>
                                      <td className="py-2 pr-3 font-mono">{pct(row.backtest.win_rate * 100, 1)}</td>
                                      <td
                                        className={`py-2 pr-3 font-mono ${
                                          row.backtest.net_pnl >= 0 ? "text-emerald-700" : "text-rose-700"
                                        }`}
                                      >
                                        {inr(row.backtest.net_pnl)}
                                      </td>
                                      <td className="py-2 pr-3 font-mono">{row.forward.trades}</td>
                                      <td className="py-2 pr-3 font-mono">{pct(row.forward.win_rate * 100, 1)}</td>
                                      <td
                                        className={`py-2 pr-3 font-mono ${
                                          row.forward.net_pnl >= 0 ? "text-emerald-700" : "text-rose-700"
                                        }`}
                                      >
                                        {inr(row.forward.net_pnl)}
                                      </td>
                                      <td className="py-2 pr-3 text-rose-700 font-mono">{inr(row.worst_loss)}</td>
                                    </tr>
                                  );
                                })}
                              </tbody>
                            </table>
                          </div>
                          <p className="text-xs text-zinc-500 italic">
                            {reviewReport.report.hard_stop_sweep.reason}
                          </p>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
