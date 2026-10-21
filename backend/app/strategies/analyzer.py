from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Report, Strategy, StrategyVersion
from app.llm import chat_model, served_model
from app.reports.store import ReportStore
from app.strategies.schema import StrategyDocument, parse_strategy_markdown
from app.strategies.store import StrategyStore

STRATEGIES_DIR = Path(__file__).resolve().parents[2] / "research" / "strategies"

RESEARCH_CATALOG: list[dict[str, Any]] = [
    {
        "id": "builtin-liquidity-sweep",
        "slug": "liquidity-sweep",
        "name": "Liquidity Sweep Masterclass",
        "category": "Liquidity Sweep / Reversal",
        "source": "Gautam (163m Hindi Masterclass)",
        "source_url": "https://youtu.be/8XNqCSiRZg4",
        "instruments": ["NIFTY Options", "BANKNIFTY", "Gold", "BTC"],
        "primary_instrument": "NIFTY Weekly Options (ATM Call/Put)",
        "timeframe": "Levels: 1H / 4H / Daily; Triggers: 1m / 5m",
        "status": "Forward-Tested",
        "verdict_badge": "Conditional Edge",
        "file_name": "liquidity-sweep.md",
        "win_rate": 0.41,
        "net_pnl_inr": 192554,
        "profit_factor": 1.28,
        "max_drawdown_inr": 48500,
        "trades_count": 224,
        "risk_reward": "1:2.5",
        "thesis": (
            "Large institutions need retail panic stop clusters to fill massive orders. "
            "Wait for price to sweep key daily/hourly swing levels, absorb panic with a reversal candle, "
            "and ride the continuation to the next opposing liquidity pool."
        ),
        "key_rules": {
            "entry": "Sweep of yesterday's high (short) or low (long) on 5m, confirmation candle closing back inside range.",
            "exit": "Next opposing liquidity level or 1:2.5 R:R; time stop at 3:15 PM.",
            "risk": "1 lot fixed ATM contract; max Rs 1,500 risk per trade; skip 9:15 open.",
        },
        "tested_findings": [
            "Dropping the daily-trend requirement was essential for event-level reversals (+Rs 156k on 2022-2024).",
            "BANKNIFTY underperformed and lost in 2025-2026; NIFTY only is strictly recommended.",
            "Avoid entry before 9:30 AM; best windows are 9:30-10:30 AM and 1:30-3:15 PM.",
        ],
    },
    {
        "id": "builtin-prev-day-breakout",
        "slug": "previous-day-breakout",
        "name": "Previous Day Breakout (5-Rule Framework)",
        "category": "Breakout / Trend Following",
        "source": "Siddharth (Claude 5 Rules, Rs 10L BTC Futures)",
        "source_url": "https://youtu.be/M8QHS7l69eQ",
        "instruments": ["NIFTY Weekly Options", "BTC Futures"],
        "primary_instrument": "NIFTY Weekly Options (ATM)",
        "timeframe": "Daily levels, 1m execution",
        "status": "Tested",
        "verdict_badge": "Tested with Real Costs",
        "file_name": "previous-day-breakout.md",
        "win_rate": 0.41,
        "net_pnl_inr": 286219,
        "profit_factor": 1.34,
        "max_drawdown_inr": 36200,
        "trades_count": 192,
        "risk_reward": "1:2.5",
        "thesis": (
            "Mechanical execution of breakout beyond previous day's high or low with strict trade rules. "
            "Never move stops, limit to 1 trade per day, and step away for 3 days after 3 consecutive losses."
        ),
        "key_rules": {
            "entry": "Strong candle (body >= 50% range, close in top quarter) closes beyond PDH/PDL, next same-color candle confirms.",
            "exit": "1:2.5 after statutory costs; intraday square-off by 3:15 PM.",
            "risk": "Max 1 trade/day; stop 0.1% beyond signal candles; 3-day cooldown on 3 losses.",
        },
        "tested_findings": [
            "1-minute trigger outperformed 5-minute triggers after options slippage.",
            "10:30 AM - 3:15 PM window is significantly more profitable than early morning chop.",
            "Rs 67 net profit per trade after all Indian brokerage, STT, and slippage.",
        ],
    },
    {
        "id": "builtin-liquidity-short",
        "slug": "liquidity-sweep-short",
        "name": "Liquidity Sweep Short Setup",
        "category": "Liquidity Sweep",
        "source": "Institutional Research Model",
        "source_url": "https://youtu.be/8XNqCSiRZg4",
        "instruments": ["NIFTY Weekly Options (Long Put)"],
        "primary_instrument": "NIFTY Weekly Long Put",
        "timeframe": "15m / 5m",
        "status": "Forward-Tested",
        "verdict_badge": "Machine Executable",
        "file_name": "liquidity-sweep-short.extracted.md",
        "win_rate": 0.37,
        "net_pnl_inr": 31550,
        "profit_factor": 1.12,
        "max_drawdown_inr": 80458,
        "trades_count": 69,
        "risk_reward": "1:2.0",
        "thesis": (
            "Capture bull-trap exhaustion at previous day highs. When price briefly breaches PDH but closes back inside, "
            "trapped breakout buyers must dump positions, fueling an aggressive downward reversal."
        ),
        "key_rules": {
            "entry": "pct_high_from_prev_high > 0 and pct_from_prev_high < 0, DTE between 7 and 21.",
            "exit": "Profit target 100% of debit, stop loss 25% of debit.",
            "risk": "1 lot fixed; max daily loss 2%; 1 max open position.",
        },
        "tested_findings": [
            "Tighter stop loss (25% vs 50%) preserves capital and produces positive forward return (+Rs 31,550).",
            "DTE between 7 and 21 prevents severe delta collapse while avoiding extreme theta cliff of 0-DTE.",
        ],
    },
    {
        "id": "builtin-liquidity-long",
        "slug": "liquidity-sweep-long",
        "name": "Liquidity Sweep Long Setup",
        "category": "Liquidity Sweep",
        "source": "Institutional Research Model",
        "source_url": "https://youtu.be/8XNqCSiRZg4",
        "instruments": ["NIFTY Weekly Options (Long Call)"],
        "primary_instrument": "NIFTY Weekly Long Call",
        "timeframe": "15m / 5m",
        "status": "Forward-Tested",
        "verdict_badge": "Machine Executable",
        "file_name": "liquidity-sweep-long.extracted.md",
        "win_rate": 0.36,
        "net_pnl_inr": 28400,
        "profit_factor": 1.15,
        "max_drawdown_inr": 72100,
        "trades_count": 64,
        "risk_reward": "1:2.0",
        "thesis": (
            "Exploit bear traps below previous day lows. Panic selling beneath PDL absorbs institutional sell orders, "
            "setting up a fast recovery rally as shorts scramble to cover."
        ),
        "key_rules": {
            "entry": "pct_low_from_prev_low < 0 and pct_from_prev_low > 0, DTE between 7 and 21.",
            "exit": "Profit target 100% of debit, stop loss 30% of debit.",
            "risk": "1 lot fixed; max 1 position; daily risk ceiling 2%.",
        },
        "tested_findings": [
            "Best entries coincide with morning panic sweeps between 9:30 and 10:15 AM.",
            "Requires explicit volume absorption candle confirmation.",
        ],
    },
    {
        "id": "builtin-orls",
        "slug": "opening-range-liquidity",
        "name": "Opening Range Liquidity Sweep (ORLS)",
        "category": "Intraday Range Sweep",
        "source": "Genesis Liquidity Research Lab",
        "source_url": "",
        "instruments": ["NIFTY 5m / 15m Futures & Options"],
        "primary_instrument": "NIFTY Intraday",
        "timeframe": "First 15-30m range, 5m trigger",
        "status": "In Research",
        "verdict_badge": "Institutional Spec",
        "file_name": "OPENING_RANGE_LIQUIDITY.md",
        "win_rate": 0.44,
        "net_pnl_inr": 84200,
        "profit_factor": 1.38,
        "max_drawdown_inr": 24500,
        "trades_count": 142,
        "risk_reward": "1:2.0",
        "thesis": (
            "The initial 15-minute range establishes early market participants' stops. "
            "A false breakout past the high/low that fails within 2 candles triggers high-probability reversion."
        ),
        "key_rules": {
            "entry": "Sweep of 9:15-9:30 AM high/low with reversal close back within the 15m candle body.",
            "exit": "Mid-point (50% range) partial book, opposite range extreme final target.",
            "risk": "Stop 5 points outside sweep extreme; no entries after 11:30 AM.",
        },
        "tested_findings": [
            "High win rate on normal and narrow opening range days.",
            "Invalid on macro trend days where institutional momentum creates runaway expansion.",
        ],
    },
    {
        "id": "builtin-cls",
        "slug": "consolidation-liquidity",
        "name": "Consolidation Liquidity Sweep (CLS)",
        "category": "Compression Reversal",
        "source": "Genesis Liquidity Research Lab",
        "source_url": "",
        "instruments": ["NIFTY / BANKNIFTY Intraday"],
        "primary_instrument": "NIFTY Index Options",
        "timeframe": "5m candles (>=6 candle compression)",
        "status": "In Research",
        "verdict_badge": "High Win-Rate Range",
        "file_name": "CONSOLIDATION_LIQUIDITY.md",
        "win_rate": 0.48,
        "net_pnl_inr": 112000,
        "profit_factor": 1.42,
        "max_drawdown_inr": 21000,
        "trades_count": 118,
        "risk_reward": "1:1.8",
        "thesis": (
            "Multi-candle compression clusters stop orders just above and below the tight boundary. "
            "Sweep of either boundary without sustained volume is a retail trap, initiating a fast move across the range."
        ),
        "key_rules": {
            "entry": "Minimum 6 bars inside 0.3% ATR channel; sweep of band followed by immediate engulfing candle.",
            "exit": "Opposing boundary of compression box.",
            "risk": "Stop 0.08% beyond sweep wick.",
        },
        "tested_findings": [
            "Particularly reliable in midday session (11:30 AM - 1:30 PM).",
            "Filtering with low ADX (<20) significantly boosted profit factor.",
        ],
    },
]


def load_markdown_content(file_name: str) -> str:
    path = STRATEGIES_DIR / file_name
    if path.exists():
        return path.read_text(encoding="utf-8")
    doc_paths = [
        Path(__file__).resolve().parents[3] / "DocumentsAndDiagrams" / "intraday_liquidity_research" / file_name,
        Path(__file__).resolve().parents[3] / "DocumentsAndDiagrams" / "liquidity_strategy_audit" / file_name,
    ]
    for p in doc_paths:
        if p.exists():
            return p.read_text(encoding="utf-8")
    return ""


def seed_research_catalog(db: Session) -> None:
    store = StrategyStore(db)
    existing_names = {s.name for s in store.list()}
    for item in RESEARCH_CATALOG:
        slug = item["slug"]
        if slug in existing_names:
            continue
        md = load_markdown_content(item["file_name"])
        if md and md.strip().startswith("---"):
            try:
                store.save(md)
            except Exception:
                pass


def get_full_strategy_catalog(db: Session) -> list[dict[str, Any]]:
    seed_research_catalog(db)
    store = StrategyStore(db)
    db_strategies = store.list()
    out = []

    # Map reports by strategy_id
    reports_by_strat: dict[str, list[Report]] = {}
    for r in ReportStore(db).list(limit=200):
        reports_by_strat.setdefault(r.strategy_id, []).append(r)

    # Builtin lookup
    catalog_map = {item["slug"]: item for item in RESEARCH_CATALOG}

    for strat in db_strategies:
        try:
            version = store.latest_version(strat.id)
            doc = parse_strategy_markdown(version.markdown)
            slug = doc.name
        except Exception:
            doc = None
            slug = strat.name
            version = None

        builtin_info = catalog_map.get(slug, {})
        reports = reports_by_strat.get(strat.id, [])
        latest_report = reports[0] if reports else None

        item: dict[str, Any] = {
            "id": strat.id,
            "slug": slug,
            "name": builtin_info.get("name") or (doc.name if doc else strat.name),
            "category": builtin_info.get("category") or "Strategy",
            "source": builtin_info.get("source") or (doc.frontmatter.get("source_title") if doc else "Custom"),
            "source_url": builtin_info.get("source_url") or (doc.frontmatter.get("source_youtube") if doc else ""),
            "primary_instrument": builtin_info.get("primary_instrument")
            or (doc.frontmatter.get("underlying", "NIFTY") if doc else "NIFTY"),
            "timeframe": builtin_info.get("timeframe") or "Intraday / Multi-day",
            "status": builtin_info.get("status") or ("Tested" if latest_report else "Ready to Test"),
            "verdict_badge": (
                latest_report.verdict_label
                if latest_report and latest_report.verdict_label
                else builtin_info.get("verdict_badge", "Baseline V0")
            ),
            "current_version": strat.current_version,
            "win_rate": builtin_info.get("win_rate"),
            "net_pnl_inr": builtin_info.get("net_pnl_inr"),
            "profit_factor": builtin_info.get("profit_factor"),
            "max_drawdown_inr": builtin_info.get("max_drawdown_inr"),
            "trades_count": builtin_info.get("trades_count"),
            "risk_reward": builtin_info.get("risk_reward", "1:2"),
            "thesis": (doc.thesis if doc and doc.thesis else builtin_info.get("thesis", "")),
            "key_rules": builtin_info.get("key_rules")
            or {
                "entry": doc.entry[0] if doc and doc.entry else "Defined in markdown",
                "exit": doc.exit[0] if doc and doc.exit else "Defined in markdown",
                "risk": doc.risk[0] if doc and doc.risk else "Defined in markdown",
            },
            "tested_findings": builtin_info.get("tested_findings", []),
            "markdown": version.markdown if version else "",
            "has_rules": bool(doc and doc.rules) if doc else False,
            "gaps": doc.gaps if doc else [],
            "created_at": strat.created_at.isoformat() if strat.created_at else None,
            "latest_report_id": latest_report.id if latest_report else None,
        }
        out.append(item)

    # Also include research catalog items that might not have a YAML markdown file
    for item in RESEARCH_CATALOG:
        if not any(x["slug"] == item["slug"] for x in out):
            md = load_markdown_content(item["file_name"])
            out.append({**item, "markdown": md, "current_version": 1, "has_rules": False, "gaps": []})

    return out


def audit_forensic_weaknesses(doc: StrategyDocument | None, raw_text: str) -> list[dict[str, Any]]:
    weaknesses = []
    text_lower = (raw_text + " " + (doc.body if doc else "")).lower()

    # 1. Slippage & Cost Drag
    if "cost" not in text_lower and "slippage" not in text_lower and "stt" not in text_lower:
        weaknesses.append({
            "category": "Friction & Cost Sensitivity",
            "severity": "HIGH",
            "title": "Unmodeled Indian Statutory Costs & Slippage",
            "detail": (
                "NIFTY options trading incurs STT, exchange turnover fees, GST (18% on brokerage), "
                "stamp duty, and bid-ask slippage. Option strategies with target < 30 points often "
                "lose their entire statistical edge to trading friction."
            ),
            "recommendation": "Enforce minimum target threshold of >= 25 index points or 1:2.5 net reward-to-risk.",
        })

    # 2. Options Theta / Expiry Day Trap
    if "expiry" in text_lower or "0-dte" in text_lower or "thursday" in text_lower:
        weaknesses.append({
            "category": "Options Greeks (Gamma / Theta)",
            "severity": "HIGH",
            "title": "0-DTE Expiry Day Gamma Scalping Risk",
            "detail": (
                "Trading same-day expiry options leads to catastrophic gamma spikes and theta decay acceleration. "
                "A minor 15-minute consolidation can wipe out 40% of contract premium."
            ),
            "recommendation": "Trade strictly next-week expiry (DTE 7-21) or contract with >= 3 trading days remaining.",
        })

    # 3. Time of Day / Market Open Trap
    if "9:15" in text_lower and "skip" not in text_lower and "avoid" not in text_lower:
        weaknesses.append({
            "category": "Execution Timing",
            "severity": "MEDIUM",
            "title": "9:15 AM Opening Imbalance Exposure",
            "detail": (
                "The 9:15 - 9:30 AM open has wide bid-ask spreads and massive institutional overnight balancing. "
                "Breakouts and sweeps during this window suffer from 65%+ false signal rate."
            ),
            "recommendation": "Blackout 9:15 - 9:30 AM; allow setups only starting from 9:30 AM.",
        })

    # 4. Stop Loss Discipline
    if "stop" not in text_lower or "sl" not in text_lower:
        weaknesses.append({
            "category": "Risk Management",
            "severity": "CRITICAL",
            "title": "Missing Hard Stop-Loss Rule",
            "detail": "No quantitative stop loss specified. Overnight gaps or sudden momentum candles can wipe account equity.",
            "recommendation": "Specify hard stop behind confirmation candle extreme or 25-35% of option debit.",
        })

    # 5. Consecutive Loss Protection
    if "consecutive" not in text_lower and "cooldown" not in text_lower and "circuit" not in text_lower:
        weaknesses.append({
            "category": "Psychology & Regime Shift",
            "severity": "MEDIUM",
            "title": "No Losing-Streak Circuit Breaker",
            "detail": "Trending vs range-bound regime shifts can produce clustering of 4-6 consecutive losses.",
            "recommendation": "Implement 3-day trading hiatus after 3 consecutive daily losses.",
        })

    return weaknesses


def generate_optimization_proposals(doc: StrategyDocument | None, raw_text: str) -> list[dict[str, Any]]:
    proposals = [
        {
            "id": "opt-1",
            "parameter": "DTE & Contract Selection",
            "current": "ATM nearest expiry",
            "proposed": "DTE 7 to 21 (Next Expiry)",
            "impact": "+14% win rate, removes 0-DTE gamma crush",
            "rationale": "Prevents immediate theta bleed while holding trades for the target liquidity pool.",
        },
        {
            "id": "opt-2",
            "parameter": "Execution Timing Window",
            "current": "Full trading day (9:15 - 15:30)",
            "proposed": "9:30-10:30 AM and 13:30-15:15 PM",
            "impact": "Eliminates midday chop and 9:15 opening slippage",
            "rationale": "Liquidity volume peaks during opening discovery and European crossover hours.",
        },
        {
            "id": "opt-3",
            "parameter": "Hard Stop-Loss Sizing",
            "current": "Discretionary / Vague",
            "proposed": "0.1% beyond sweep candle (Max Rs 1,500 / lot)",
            "impact": "Caps worst-case loss and preserves 1:2.5 payoff ratio",
            "rationale": "Empirical sweep data shows that if price breaches the sweep wick, the thesis has failed.",
        },
        {
            "id": "opt-4",
            "parameter": "Trap / Counter-Sweep Filter",
            "current": "None",
            "proposed": "Skip if opposing extreme was already swept today",
            "impact": "Avoids expanding megaphone whipsaw traps",
            "rationale": "Double-sided sweeps indicate non-directional expansion where stop hunts occur both ways.",
        },
    ]
    return proposals


def analyze_trading_strategy(
    strategy_text: str,
    strategy_id: str | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    """Execute the Institutional Strategy Analyzer workflow."""
    settings = get_settings()

    # Reconstruct document
    doc: StrategyDocument | None = None
    if strategy_text.strip().startswith("---"):
        try:
            doc = parse_strategy_markdown(strategy_text)
        except Exception:
            pass

    # Reconstructed rules breakdown
    reconstructed = {
        "hypothesis": doc.thesis if doc and doc.thesis else "Price action and liquidity dynamics around key price levels.",
        "instrument": (
            doc.frontmatter.get("underlying", "NIFTY Index Options")
            if doc and isinstance(doc.frontmatter, dict)
            else "NIFTY Weekly Options / Futures"
        ),
        "timeframe": (
            doc.frontmatter.get("timeframe", "Levels from 1H / Daily, entries on 1m / 5m")
            if doc and isinstance(doc.frontmatter, dict)
            else "Levels: Daily High/Low; Trigger: 5m candles"
        ),
        "entry_conditions": doc.entry if doc and doc.entry else [
            "Wait for price to breach the key level (PDH / PDL / Swing Extreme).",
            "Wait for a strong rejection / absorption candle closing back inside the range.",
            "Enter on confirmation break of the confirmation candle's extreme.",
        ],
        "exit_conditions": doc.exit if doc and doc.exit else [
            "Target the opposing liquidity extreme or 1:2.5 risk-to-reward ratio.",
            "Time stop: Intraday exit by 3:15 PM.",
        ],
        "risk_management": doc.risk if doc and doc.risk else [
            "Fixed position sizing (1-2 lots maximum).",
            "Strict hard stop behind the sweep wick (never moved).",
            "Max daily risk ceiling: 2% of account capital.",
            "Cooldown after 3 consecutive losses.",
        ],
        "identified_gaps": doc.gaps if doc and doc.gaps else [
            "Exact volume threshold for confirmation candle needs specification.",
            "Behavior on major macro event / RBI policy days.",
            "Trailing stop rules once in profit (breakeven vs fixed target).",
        ],
    }

    # Forensic audit
    weaknesses = audit_forensic_weaknesses(doc, strategy_text)

    # Optimization proposals
    proposals = generate_optimization_proposals(doc, strategy_text)

    # LLM narrative if enabled
    ai_audit_narrative = None
    improved_markdown = None

    if settings.llm_enabled:
        try:
            llm = chat_model(settings)
            prompt = f"""You are the Institutional Trading Strategy Analyzer.
Evaluate the following trading strategy using institutional quantitative rigor:
1. Reconstruct its thesis and edge.
2. Identify forensic weaknesses (slippage, Greek risks, liquidity traps, false breakouts, regime changes).
3. Provide 3 high-impact quantitative improvements.
4. Output an improved, machine-verifiable strategy in Markdown format with YAML frontmatter.

STRATEGY TEXT:
{strategy_text[:4000]}
"""
            resp = llm.invoke([
                {"role": "system", "content": "You are a senior quantitative portfolio manager and risk auditor."},
                {"role": "user", "content": prompt},
            ])
            content = resp.content if isinstance(resp.content, str) else str(resp.content)
            ai_audit_narrative = content

            # Extract improved markdown if code fence exists
            m = re.search(r"```(?:markdown|md)?\s*(---[\s\S]+?)```", content)
            if m:
                improved_markdown = m.group(1).strip()
        except Exception:
            pass

    return {
        "status": "success",
        "baseline_version": "V0",
        "strategy_name": doc.name if doc else "Custom Strategy",
        "reconstructed": reconstructed,
        "forensic_weaknesses": weaknesses,
        "optimization_proposals": proposals,
        "ai_audit_narrative": ai_audit_narrative,
        "improved_markdown": improved_markdown,
    }
