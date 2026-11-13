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
from app.strategies.normalizer import (
    normalize_pdc_15ema_v0_benchmark,
    normalize_scanner_v0_family,
    normalize_strategy_document,
)
from app.strategies.schema import StrategyDocument, parse_strategy_markdown
from app.strategies.store import StrategyStore
from app.trading.paper_registry import FAMILIES as SCANNER_V0_FAMILIES

STRATEGIES_DIR = Path(__file__).resolve().parents[2] / "research" / "strategies"

RESEARCH_CATALOG: list[dict[str, Any]] = [
    {
        "id": "builtin-liquidity-sweep",
        "slug": "liquidity-sweep",
        "name": "Liquidity Sweep Strategy",
        "category": "Liquidity Sweep / Reversal",
        "source": "Institutional Price Action",
        "source_url": "",
        "video_details": {
            "title": "Liquidity Masterclass (Hindi Live Session, 163 min)",
            "channel_or_speaker": "Gautam",
            "url": "https://youtu.be/8XNqCSiRZg4",
            "duration": "163 min",
            "timestamps": [
                {"time": "75:45", "label": "Sweep Rule: Don't buy break of level, wait for sweep beyond"},
                {"time": "76:18", "label": "Reversal Confirmation: Strong opposite absorption candle"},
                {"time": "77:33", "label": "Entry Trigger: Close of confirmation candle or break of its high/low"},
                {"time": "77:40", "label": "Stop Placement: 0.1% behind confirmation candle / sweep extreme"},
                {"time": "93:30", "label": "Liquidity Concept: Retail panic stops cluster beyond swing highs/lows"},
                {"time": "95:44", "label": "Timeframe Hierarchy: Higher timeframe levels yield higher accuracy"},
                {"time": "115:15", "label": "Session Timing: Avoid 9:15 AM Indian market open chop"},
                {"time": "136:26", "label": "Event-Level Reversals: 1H / 4H / Daily swing setups on 5m chart"},
            ],
        },
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
        "source": "5-Rule Breakout Framework",
        "source_url": "",
        "video_details": {
            "title": "5 Rules I Gave Claude (30 Days of Bitcoin Futures, Rs 10 Lakh)",
            "channel_or_speaker": "Siddharth",
            "url": "https://youtu.be/M8QHS7l69eQ",
            "duration": "Recorded Case Study",
            "timestamps": [
                {"time": "02:15", "label": "Core Level: Previous day's high & low (PDH/PDL)"},
                {"time": "05:40", "label": "Strict Limit: At most 1 trade per day"},
                {"time": "08:10", "label": "Pre-trade Discipline: Write down entry, SL, and target before executing"},
                {"time": "12:30", "label": "Fixed Risk: Never move stops after entry; max 0.5% risk"},
                {"time": "18:45", "label": "Psychological Circuit Breaker: 3-day mandatory cooldown after 3 consecutive losses"},
            ],
        },
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
        "source_url": "",
        "video_details": {
            "title": "Liquidity Sweep Short Setup (Gautam Masterclass)",
            "channel_or_speaker": "Gautam",
            "url": "https://youtu.be/8XNqCSiRZg4",
            "duration": "163 min",
            "timestamps": [
                {"time": "76:18", "label": "Bear trap & panic absorption candle confirmation"},
                {"time": "115:15", "label": "Skip 9:15 open"},
                {"time": "128:51", "label": "Short entry trigger on confirmation low break"},
            ],
        },
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
        "source_url": "",
        "video_details": {
            "title": "Liquidity Sweep Long Setup (Gautam Masterclass)",
            "channel_or_speaker": "Gautam",
            "url": "https://youtu.be/8XNqCSiRZg4",
            "duration": "163 min",
            "timestamps": [
                {"time": "76:18", "label": "Bull trap / panic flush absorption candle confirmation"},
                {"time": "115:15", "label": "Skip 9:15 open"},
                {"time": "136:26", "label": "Event-level reversals on 5m chart"},
            ],
        },
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
    {
        "id": "builtin-trendline-breakout",
        "slug": "trendline-breakout",
        "name": "3rd-Point Trendline Breakout (Higher Timeframe)",
        "category": "Trendline Reversal / Liquidity Trap",
        "source": "Genesis Liquidity Research Lab",
        "source_url": "",
        "video_details": None,
        "instruments": ["NIFTY Weekly Options (ATM CE/PE)"],
        "primary_instrument": "NIFTY Weekly Options (ATM CE/PE)",
        "timeframe": "5m structure, optional 1m execution",
        "status": "Backtested & Live",
        "verdict_badge": "High Edge (PF 3.32)",
        "file_name": "trendline-breakout.md",
        "win_rate": 0.479,
        "net_pnl_inr": 169412,
        "profit_factor": 3.32,
        "max_drawdown_inr": 28500,
        "trades_count": 71,
        "risk_reward": "1:2.5 to 1:3.0",
        "thesis": (
            "At the 3rd point of a mature 5m trendline, retail orders cluster to play the continuation bounce. "
            "A clean breakout/breakdown beyond the line triggers massive stop-loss cascades. "
            "Entering on the 5m close (or 1m micro-trigger at the line) captures asymmetric trend reversals."
        ),
        "key_rules": {
            "entry": "5m candle closes strictly outside trendline (slope >= 0.5) with color alignment (Red for short, Green for long).",
            "exit": "Staged exits: +1R move SL to Breakeven, +2R book 50% lots, +3R exit runner; intraday square-off at 15:10 IST.",
            "risk": "Risk capped at 15-65 index points; max 5% capital per trade; max 1 trade per direction per day; 3-day cooldown on 3 losses.",
        },
        "tested_findings": [
            "Generated +807.8 index points (+21.12 R) across 60 days of 5m NIFTY data.",
            "Adding RSI trendline or overbought filters destroys edge (dropping profit to -116 pts).",
            "Waiting for a 2nd confirmation candle (C2) collapsed win rate to 25% (-314 pts); C1 close IS the confirmation.",
            "1m micro-trigger at the 5m line reduces stop loss risk by up to 66% (e.g. 15 pts vs 45 pts).",
        ],
    },
]


def sanitize_strategy_specification(text: str) -> str:
    """Strip all video details, YouTube links, and video timestamps so only strategy rules/thesis remain."""
    if not text:
        return ""
    # Strip HTML comment tags referencing source / timestamps
    text = re.sub(r'<!--\s*source:[^>]*-->', '', text, flags=re.IGNORECASE)
    # Strip video timestamps like [93:30] or [1:23:45]
    text = re.sub(r'\[\d{1,3}:\d{2}(?::\d{2})?\]', '', text)
    # Strip YAML frontmatter fields for video / youtube
    text = re.sub(r'^\s*source_youtube:.*$', '', text, flags=re.MULTILINE | re.IGNORECASE)
    text = re.sub(r'^\s*source_title:.*$', '', text, flags=re.MULTILINE | re.IGNORECASE)
    text = re.sub(r'^\s*instruments_in_source:\s*\n(?:\s*-\s*.*\n?)*', '', text, flags=re.MULTILINE | re.IGNORECASE)
    text = re.sub(r'^\s*instruments_in_source:.*$', '', text, flags=re.MULTILINE | re.IGNORECASE)
    # Strip inline video references
    text = re.sub(r'\s*\((?:from the video|as the video)\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\bvideo\s*\[\d+:\d+\]', '', text, flags=re.IGNORECASE)
    # Clean excessive empty lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


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
    """Seed missing research catalog markdown strategies without mutating or migrating existing versions."""
    store = StrategyStore(db)
    existing_names: set[str] = set()
    for s in store.list():
        existing_names.add(s.name)
        try:
            ver = store.latest_version(s.id)
            doc = parse_strategy_markdown(ver.markdown)
            existing_names.add(doc.name)
        except Exception:
            pass

    for item in RESEARCH_CATALOG:
        slug = item["slug"]
        if slug in existing_names:
            continue
        md = load_markdown_content(item["file_name"])
        if not md or not md.strip().startswith("---"):
            continue
        try:
            # Preserve full provenance (source_youtube and <!-- source: ... -->) in stored markdown
            saved = store.save(md)
            existing_names.add(slug)
            existing_names.add(saved.strategy_id)
            db.commit()
        except Exception:
            db.rollback()


def _verified_metrics_from_report(report: Report | None) -> dict[str, Any]:
    """Extract verified simulation metrics from a persisted Report, or None when unverified."""
    if report is None or report.status != "completed" or report.verdict_label == "Not executable":
        return {
            "metrics_provenance": "NO_VERIFIED_REPORT",
            "win_rate": None,
            "net_pnl_inr": None,
            "profit_factor": None,
            "max_drawdown_inr": None,
            "trades_count": None,
        }
    payload = report.payload if isinstance(report.payload, dict) else {}
    verdict = payload.get("verdict") if isinstance(payload.get("verdict"), dict) else {}
    best = verdict.get("best_scenario") if isinstance(verdict.get("best_scenario"), dict) else None
    if not best:
        results = payload.get("scenario_results")
        best = results[0] if isinstance(results, list) and results else None
    if not isinstance(best, dict):
        return {
            "metrics_provenance": "VERIFIED_REPORT_EMPTY",
            "win_rate": None,
            "net_pnl_inr": None,
            "profit_factor": None,
            "max_drawdown_inr": None,
            "trades_count": None,
        }
    bt = best.get("backtest") if isinstance(best.get("backtest"), dict) else {}
    fw = best.get("forward") if isinstance(best.get("forward"), dict) else {}
    bt_trades = int(bt.get("trades") or 0)
    fw_trades = int(fw.get("trades") or 0)
    total_trades = bt_trades + fw_trades
    net_pnl = float(bt.get("net_pnl") or 0.0) + float(fw.get("net_pnl") or 0.0)
    max_dd = max(float(bt.get("max_drawdown") or 0.0), float(fw.get("max_drawdown") or 0.0))
    win_rate = (
        round(((float(bt.get("win_rate") or 0.0) * bt_trades) + (float(fw.get("win_rate") or 0.0) * fw_trades)) / total_trades, 4)
        if total_trades > 0
        else None
    )
    return {
        "metrics_provenance": "VERIFIED_REPORT",
        "win_rate": win_rate,
        "net_pnl_inr": round(net_pnl, 2),
        "profit_factor": None,
        "max_drawdown_inr": round(max_dd, 2),
        "trades_count": total_trades,
    }


def get_full_strategy_catalog(db: Session) -> list[dict[str, Any]]:
    seed_research_catalog(db)
    store = StrategyStore(db)
    db_strategies = store.list()
    out: list[dict[str, Any]] = []
    seen_identities: set[str] = set()

    # Map reports by strategy_id
    reports_by_strat: dict[str, list[Report]] = {}
    for r in ReportStore(db).list(limit=200):
        reports_by_strat.setdefault(r.strategy_id, []).append(r)

    for strat in db_strategies:
        try:
            version = store.latest_version(strat.id)
            doc = parse_strategy_markdown(version.markdown)
            slug = doc.name
        except Exception:
            doc = None
            slug = strat.name
            version = None

        seen_identities.add(slug)

        # A matching display name is not evidence that a strategy uses catalog rules.
        builtin_info = {}
        reports = reports_by_strat.get(strat.id, [])
        latest_report = next((r for r in reports if r.status == "completed"), reports[0] if reports else None)
        has_validated_report = False

        norm = (
            normalize_strategy_document(
                doc,
                strategy_id=strat.id,
                strategy_version=f"v{strat.current_version}",
                has_validated_report=has_validated_report,
            )
            if doc is not None
            else None
        )
        from app.strategies.normalizer import has_bound_oos_validation
        if norm is not None and latest_report is not None:
            has_validated_report = has_bound_oos_validation(latest_report.payload, norm)
            if has_validated_report:
                norm = normalize_strategy_document(
                    doc, strategy_id=strat.id, strategy_version=f"v{strat.current_version}",
                    has_validated_report=True, validation_fidelity_tier="TIER_1_VERIFIED")
        verified_metrics = _verified_metrics_from_report(latest_report if has_validated_report else None)
        unverified_claims = (
            {
                "win_rate": builtin_info.get("win_rate"),
                "net_pnl_inr": builtin_info.get("net_pnl_inr"),
                "profit_factor": builtin_info.get("profit_factor"),
                "max_drawdown_inr": builtin_info.get("max_drawdown_inr"),
                "trades_count": builtin_info.get("trades_count"),
            }
            if builtin_info
            else None
        )

        category_class = norm.category_class if norm is not None else "INCOMPLETE_SPECIFICATION"
        promotion_state = norm.promotion_state if norm is not None else "QUARANTINED"
        quarantined = norm.quarantined if norm is not None else True
        quarantine_reasons = list(norm.quarantine_reasons) if norm is not None else ["INVALID_STRATEGY_DOCUMENT"]

        verdict_badge = (
            latest_report.verdict_label
            if latest_report and latest_report.verdict_label
            else ("Quarantined" if quarantined else "Normalized")
        )

        item: dict[str, Any] = {
            "id": strat.id,
            "slug": slug,
            "name": builtin_info.get("name") or (doc.name if doc else strat.name),
            "category": builtin_info.get("category") or "Strategy",
            "category_class": category_class,
            "promotion_state": promotion_state,
            "quarantined": quarantined,
            "quarantine_reasons": quarantine_reasons,
            "simulatable": norm.simulatable if norm is not None else False,
            "paper_eligible": norm.paper_eligible if norm is not None else False,
            "promotable": norm.promotable if norm is not None else False,
            "spec_sha256": norm.spec_sha256 if norm is not None else None,
            "rules_v2_sha256": norm.rules_v2_sha256 if norm is not None else None,
            "parameter_hash": norm.parameter_hash if norm is not None else None,
            "source": builtin_info.get("source") or "Institutional Model",
            "source_url": builtin_info.get("source_url") or "",
            "video_details": builtin_info.get("video_details"),
            "primary_instrument": norm.rules_v2.instrument.underlying if norm and norm.rules_v2 else "Unspecified",
            "timeframe": norm.rules_v2.timeframes.primary if norm and norm.rules_v2 else "Unspecified",
            "status": (
                "Quarantined"
                if quarantined
                else ("Validated" if category_class == "VALIDATED" else "Normalized")
            ),
            "verdict_badge": verdict_badge,
            "current_version": strat.current_version,
            "metrics_provenance": verified_metrics["metrics_provenance"],
            "win_rate": verified_metrics["win_rate"],
            "net_pnl_inr": verified_metrics["net_pnl_inr"],
            "profit_factor": verified_metrics["profit_factor"],
            "max_drawdown_inr": verified_metrics["max_drawdown_inr"],
            "trades_count": verified_metrics["trades_count"],
            "unverified_research_claims": unverified_claims,
            "risk_reward": doc.frontmatter.get("risk_reward", "Unspecified") if doc else "Unspecified",
            "thesis": (doc.thesis if doc and doc.thesis else builtin_info.get("thesis", "")),
            "key_rules": builtin_info.get("key_rules")
            or {
                "entry": doc.entry[0] if doc and doc.entry else "Defined in markdown",
                "exit": doc.exit[0] if doc and doc.exit else "Defined in markdown",
                "risk": doc.risk[0] if doc and doc.risk else "Defined in markdown",
            },
            "tested_findings": builtin_info.get("tested_findings", []),
            "markdown": sanitize_strategy_specification(version.markdown) if version else "",
            "has_rules": bool(doc and doc.rules) if doc else False,
            "gaps": doc.gaps if doc else [],
            "created_at": strat.created_at.isoformat() if strat.created_at else None,
            "latest_report_id": latest_report.id if latest_report else None,
        }
        out.append(item)

    # Include non-YAML research hypotheses from RESEARCH_CATALOG (explicitly quarantined as RESEARCH_HYPOTHESIS)
    for item in RESEARCH_CATALOG:
        slug = item["slug"]
        if slug in seen_identities:
            continue
        seen_identities.add(slug)
        md = load_markdown_content(item["file_name"])
        clean_md = sanitize_strategy_specification(md)
        unverified_claims = {
            "win_rate": item.get("win_rate"),
            "net_pnl_inr": item.get("net_pnl_inr"),
            "profit_factor": item.get("profit_factor"),
            "max_drawdown_inr": item.get("max_drawdown_inr"),
            "trades_count": item.get("trades_count"),
        }
        out.append(
            {
                **item,
                "category_class": "RESEARCH_HYPOTHESIS",
                "promotion_state": "QUARANTINED",
                "quarantined": True,
                "quarantine_reasons": [
                    "RESEARCH_HYPOTHESIS_ONLY: non-YAML narrative research document without machine-executable rules"
                ],
                "simulatable": False,
                "paper_eligible": False,
                "promotable": False,
                "spec_sha256": None,
                "rules_v2_sha256": None,
                "parameter_hash": None,
                "status": "Research Hypothesis (Quarantined)",
                "verdict_badge": "Quarantined (Research Hypothesis)",
                "metrics_provenance": "UNVERIFIED_STATIC_NOTES",
                "win_rate": None,
                "net_pnl_inr": None,
                "profit_factor": None,
                "max_drawdown_inr": None,
                "trades_count": None,
                "unverified_research_claims": unverified_claims,
                "markdown": clean_md,
                "current_version": 1,
                "has_rules": False,
                "gaps": ["Non-YAML research hypothesis; requires StrategyRulesV2 specification"],
            }
        )

    # Include registered scanner-v0 IMPLEMENTED families
    for family_id in SCANNER_V0_FAMILIES:
        slug = f"scanner-v0-{family_id.lower().replace('_', '-')}"
        if slug in seen_identities or family_id in seen_identities:
            continue
        seen_identities.add(slug)
        seen_identities.add(family_id)
        norm = normalize_scanner_v0_family(family_id)
        out.append(
            {
                "id": f"implemented-{family_id.lower()}",
                "slug": slug,
                "strategy_id": family_id,
                "name": norm.name,
                "category": "Intraday Liquidity (scanner-v0)",
                "category_class": norm.category_class,
                "promotion_state": norm.promotion_state,
                "quarantined": norm.quarantined,
                "quarantine_reasons": list(norm.quarantine_reasons),
                "simulatable": norm.simulatable,
                "paper_eligible": norm.paper_eligible,
                "promotable": norm.promotable,
                "spec_sha256": norm.spec_sha256,
                "rules_v2_sha256": norm.rules_v2_sha256,
                "parameter_hash": norm.parameter_hash,
                "source": "Pinned Code (scanner-v0)",
                "source_url": "",
                "video_details": None,
                "primary_instrument": "NIFTY Weekly Options",
                "timeframe": "5m closed-bar",
                "status": "Implemented (Paper-Eligible)",
                "verdict_badge": "Implemented (scanner-v0)",
                "current_version": 1,
                "metrics_provenance": "NO_VERIFIED_REPORT",
                "win_rate": None,
                "net_pnl_inr": None,
                "profit_factor": None,
                "max_drawdown_inr": None,
                "trades_count": None,
                "unverified_research_claims": None,
                "risk_reward": "1:2.0",
                "thesis": f"Registered scanner-v0 intraday setup family ({family_id}).",
                "key_rules": {
                    "entry": f"Closed-bar 5m predicate ({norm.rules_v2.intraday_entry[0].kind if norm.rules_v2 else ''})",
                    "exit": "1:2.0 R target with 50% partial at +1R and 15:15 square-off",
                    "risk": "paper-risk-v2 (Rs 50,000 capital, max 3 lots, 2% stop budget)",
                },
                "tested_findings": [],
                "markdown": "",
                "has_rules": True,
                "gaps": [],
                "created_at": None,
                "latest_report_id": None,
            }
        )

    # Include frozen PDC Zone + 15 EMA V0 benchmark explicitly as FROZEN_BENCHMARK
    pdc_norm = normalize_pdc_15ema_v0_benchmark()
    if pdc_norm.strategy_id not in seen_identities and "pdc-15ema-v0" not in seen_identities:
        seen_identities.add(pdc_norm.strategy_id)
        seen_identities.add("pdc-15ema-v0")
        out.append(
            {
                "id": "frozen-benchmark-pdc-15ema-v0",
                "slug": "pdc-15ema-v0",
                "strategy_id": pdc_norm.strategy_id,
                "name": pdc_norm.name,
                "category": "Frozen Benchmark",
                "category_class": pdc_norm.category_class,
                "promotion_state": pdc_norm.promotion_state,
                "quarantined": pdc_norm.quarantined,
                "quarantine_reasons": list(pdc_norm.quarantine_reasons),
                "simulatable": pdc_norm.simulatable,
                "paper_eligible": pdc_norm.paper_eligible,
                "promotable": pdc_norm.promotable,
                "spec_sha256": pdc_norm.spec_sha256,
                "rules_v2_sha256": pdc_norm.rules_v2_sha256,
                "parameter_hash": pdc_norm.parameter_hash,
                "source": "Frozen Stage A Benchmark (scripts/backtest_pdc_15ema.py)",
                "source_url": "",
                "video_details": None,
                "primary_instrument": "NIFTY Weekly Options (ATM)",
                "timeframe": "5m (09:15-10:30 IST)",
                "status": "Frozen Benchmark (Disabled for Paper/Live)",
                "verdict_badge": "Frozen Benchmark (Card Divergence)",
                "current_version": 0,
                "metrics_provenance": "FROZEN_BENCHMARK_REFERENCE",
                "win_rate": None,
                "net_pnl_inr": None,
                "profit_factor": None,
                "max_drawdown_inr": None,
                "trades_count": None,
                "unverified_research_claims": None,
                "risk_reward": "1:2.0",
                "thesis": "Frozen V0 benchmark retained for historical regression comparison only.",
                "key_rules": {
                    "entry": "PDC +/-0.05% zone reaction with 15 EMA confirmation (09:15-10:30 IST)",
                    "exit": "1:2.0 R target or wick-touch stop loss (10-30 index points)",
                    "risk": "1 lot fixed; disabled in paper_registry due to 7 documented card divergences",
                },
                "tested_findings": [],
                "markdown": "",
                "has_rules": True,
                "gaps": list(pdc_norm.unresolved_gaps),
                "created_at": None,
                "latest_report_id": None,
            }
        )

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
        "hypothesis": doc.thesis if doc and doc.thesis else "Unspecified",
        "instrument": (
            doc.frontmatter.get("underlying", "Unspecified")
            if doc and isinstance(doc.frontmatter, dict)
            else "Unspecified"
        ),
        "timeframe": (
            doc.frontmatter.get("timeframe", "Unspecified")
            if doc and isinstance(doc.frontmatter, dict)
            else "Unspecified"
        ),
        "entry_conditions": doc.entry if doc else [],
        "exit_conditions": doc.exit if doc else [],
        "risk_management": doc.risk if doc else [],
        "identified_gaps": doc.gaps if doc else ["Missing deterministic strategy specification"],
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
