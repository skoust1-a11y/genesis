from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.review_crew.reviewer import REVIEWER
from app.audit import current_actor, current_request_id, record_event
from app.db.hashing import canonical_json, money, normalize_json, sha256_hex
from app.db.models import Report, SimTrade, SimulationRun, StrategyImprovement, StrategyVersion, TradeReview
from app.engine.sim import SimResult, outcome_label

ZERO = Decimal("0")

TRADE_HASH_FIELDS = (
    "report_id", "strategy_version_id", "seq", "trade_date",
    "entry_rule", "entry_conditions", "reason_text", "edge", "market_snapshot",
    "lots", "credit", "max_loss", "notional", "orders", "cost_breakdown",
    "exit_rule", "exit_reason", "holding_bars",
    "target_pnl", "stop_loss_pnl", "risk_reward",
    "gross", "costs", "net", "pnl_vs_target", "cost_ratio", "outcome_label",
    "equity_after", "drawdown_after",
)  # fmt: skip
TRADE_HASH_FIELDS_V3 = (*TRADE_HASH_FIELDS, "legs", "expiry", "exit_date")


def trade_hash_fields(run: SimulationRun) -> tuple[str, ...]:
    """Runs written before migration 0005 (no data_source) hash with the legacy field list."""
    return TRADE_HASH_FIELDS if run.data_source is None else TRADE_HASH_FIELDS_V3

# live_trade_id was dropped from trade_reviews in 0004 but stays here (always null) so the
# reviews_sha256 of reports written before that migration still recomputes.
REVIEW_HASH_FIELDS = (
    "trade_kind", "sim_trade_id", "live_trade_id", "reviewer_type", "reviewer", "rules_version",
    "followed_plan", "process_verdict", "what_went_well", "what_went_wrong", "mistake_tags",
    "lesson", "improvement_suggestion", "suggested_rule_change", "confidence",
)  # fmt: skip

IMPROVEMENT_HASH_FIELDS = (
    "strategy_id", "from_version_id", "origin", "report_id", "title", "rationale",
    "proposed_change", "evidence", "expected_impact", "proposed_by",
)  # fmt: skip


def rows_sha256(rows: list[Any], fields: tuple[str, ...]) -> str:
    """Hash ORM rows (or dicts) over a fixed field list; recomputable from stored rows."""

    def value(row, f):
        return row.get(f) if isinstance(row, dict) else getattr(row, f, None)

    return sha256_hex(canonical_json([{f: value(r, f) for f in fields} for r in rows]))


def build_trade_rows(result: SimResult, report_id: str, strategy_version_id: int) -> list[dict]:
    """Quantize to 4dp at the storage boundary so trades reconcile exactly with run totals."""
    rows: list[dict] = []
    equity = ZERO
    peak = ZERO
    for seq, trade in enumerate(result.trades, start=1):
        components = {k: money(v) for k, v in asdict(trade.cost_breakdown).items()}
        costs = sum(components.values(), ZERO)
        gross = money(trade.gross)
        net = gross - costs
        target = money(trade.target_pnl)
        equity += net
        peak = max(peak, equity)
        rows.append(
            {
                "report_id": report_id,
                "strategy_version_id": strategy_version_id,
                "seq": seq,
                "trade_date": trade.date,
                "entry_rule": trade.entry_rule,
                "entry_conditions": normalize_json([asdict(c) for c in trade.entry_conditions]),
                "reason_text": trade.reason_text,
                "edge": trade.edge,
                "market_snapshot": normalize_json(trade.market_snapshot),
                "lots": trade.lots,
                "credit": money(trade.credit),
                "max_loss": money(trade.max_loss),
                "notional": money(trade.notional),
                "orders": trade.orders,
                "cost_breakdown": {k: str(v) for k, v in components.items()},
                "exit_rule": trade.exit_rule,
                "exit_reason": trade.exit_reason,
                "holding_bars": trade.holding_bars,
                "target_pnl": target,
                "stop_loss_pnl": money(trade.stop_loss_pnl),
                "risk_reward": trade.risk_reward,
                "gross": gross,
                "costs": costs,
                "net": net,
                "pnl_vs_target": net - target,
                "cost_ratio": float(costs / abs(gross)) if gross else None,
                "outcome_label": outcome_label(float(net), float(target)),
                "equity_after": equity,
                "drawdown_after": peak - equity,
                "legs": normalize_json(trade.legs),
                "expiry": trade.expiry,
                "exit_date": trade.exit_date,
            }
        )
    return rows


class ReportStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_success(
        self,
        *,
        strategy_id: str,
        version: StrategyVersion,
        payload: dict,
        sim_runs: list[dict],
        reviews: list[dict],
        improvements: list[dict],
        inputs: dict,
        started_at: datetime,
        report_id: str | None = None,
    ) -> Report:
        payload = normalize_json(payload)
        verdict = payload.get("verdict") or {}
        report = Report(
            **({"id": report_id} if report_id else {}),
            strategy_id=strategy_id,
            strategy_version_id=version.id,
            status="completed",
            verdict_label=verdict.get("label"),
            scenarios_tested=verdict.get("scenarios_tested"),
            scenarios_passing=verdict.get("scenarios_passing"),
            inputs=normalize_json(inputs),
            payload=payload,
            payload_sha256=sha256_hex(canonical_json(payload)),
            actor=current_actor(),
            request_id=current_request_id(),
            started_at=started_at,
        )
        self.session.add(report)
        self.session.flush()

        pass_by_scenario = {
            i: bool(r.get("pass_net")) for i, r in enumerate(payload.get("scenario_results") or [])
        }
        trades_by_key: dict[tuple, SimTrade] = {}
        run_summaries = []
        for run in sim_runs:
            idx, mode, result = run["scenario_index"], run["mode"], run["result"]
            rows = build_trade_rows(result, report.id, version.id)
            wins = sum(1 for r in rows if r["net"] > 0)
            sim_run = SimulationRun(
                report_id=report.id,
                scenario_index=idx,
                scenario=normalize_json(asdict(result.scenario)),
                mode=mode,
                window_start=result.window_start,
                window_end=result.window_end,
                bars_in_window=result.bars_in_window,
                trade_count=len(rows),
                gross_pnl=sum((r["gross"] for r in rows), ZERO),
                total_costs=sum((r["costs"] for r in rows), ZERO),
                net_pnl=sum((r["net"] for r in rows), ZERO),
                win_rate=(wins / len(rows)) if rows else 0.0,
                max_drawdown=max((r["drawdown_after"] for r in rows), default=ZERO),
                pass_net=pass_by_scenario.get(idx, False),
                trades_sha256=rows_sha256(rows, TRADE_HASH_FIELDS_V3),
                data_source=result.data_source or "unknown",
                equity_curve=normalize_json(result.equity_curve),
                max_drawdown_mtm=money(result.max_drawdown_mtm),
                max_drawdown_days=result.max_drawdown_days,
                by_year=normalize_json(result.by_year),
                skipped=normalize_json(result.skipped),
            )
            self.session.add(sim_run)
            self.session.flush()
            trades = [SimTrade(simulation_run_id=sim_run.id, **row) for row in rows]
            self.session.add_all(trades)
            self.session.flush()
            for trade in trades:
                trades_by_key[(idx, mode, trade.seq)] = trade
            run_summaries.append(
                {
                    "simulation_run_id": sim_run.id,
                    "scenario_index": idx,
                    "mode": mode,
                    "trade_count": sim_run.trade_count,
                    "net_pnl": sim_run.net_pnl,
                    "trades_sha256": sim_run.trades_sha256,
                }
            )

        review_rows = []
        for review in reviews:
            trade = trades_by_key[tuple(review["key"])]
            fields = {k: v for k, v in review.items() if k != "key"}
            fields["mistake_tags"] = normalize_json(fields.get("mistake_tags") or [])
            fields["suggested_rule_change"] = normalize_json(fields.get("suggested_rule_change"))
            review_rows.append(TradeReview(trade_kind="sim", sim_trade_id=trade.id, **fields))
        self.session.add_all(review_rows)

        improvement_rows = []
        for proposal in improvements:
            evidence = dict(proposal["evidence"])
            keys = evidence.pop("trade_keys", [])
            evidence["trade_ids"] = [trades_by_key[tuple(k)].id for k in keys]
            improvement_rows.append(
                StrategyImprovement(
                    strategy_id=strategy_id,
                    from_version_id=version.id,
                    origin="report",
                    report_id=report.id,
                    title=proposal["title"][:200],
                    rationale=proposal["rationale"],
                    proposed_change=normalize_json(proposal["proposed_change"]),
                    evidence=normalize_json(evidence),
                    expected_impact=proposal.get("expected_impact"),
                    proposed_by=f"agent:{REVIEWER}",
                )
            )
        self.session.add_all(improvement_rows)
        self.session.flush()

        record_event(
            self.session,
            "report.created",
            "report",
            report.id,
            {
                "strategy_id": strategy_id,
                "strategy_version_id": version.id,
                "verdict_label": report.verdict_label,
                "payload_sha256": report.payload_sha256,
                "inputs_sha256": sha256_hex(canonical_json(report.inputs)),
                "simulation_runs": run_summaries,
                "trade_count": len(trades_by_key),
                "review_count": len(review_rows),
                "reviews_sha256": rows_sha256(review_rows, REVIEW_HASH_FIELDS),
                "improvement_count": len(improvement_rows),
                "improvements_sha256": rows_sha256(improvement_rows, IMPROVEMENT_HASH_FIELDS),
            },
        )
        return report

    def create_failed(
        self,
        *,
        strategy_id: str,
        version: StrategyVersion,
        error: str,
        inputs: dict,
        started_at: datetime,
    ) -> Report:
        report = Report(
            strategy_id=strategy_id,
            strategy_version_id=version.id,
            status="failed",
            inputs=normalize_json(inputs),
            error=error,
            actor=current_actor(),
            request_id=current_request_id(),
            started_at=started_at,
        )
        self.session.add(report)
        self.session.flush()
        record_event(
            self.session,
            "report.failed",
            "report",
            report.id,
            {"strategy_id": strategy_id, "strategy_version_id": version.id, "error": error},
        )
        return report

    def get(self, report_id: str) -> Report:
        report = self.session.get(Report, report_id)
        if report is None:
            raise FileNotFoundError(report_id)
        return report

    def list(
        self, strategy_id: str | None = None, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[Report]:
        stmt = select(Report)
        if strategy_id:
            stmt = stmt.where(Report.strategy_id == strategy_id)
        if status:
            stmt = stmt.where(Report.status == status)
        stmt = stmt.order_by(Report.started_at.desc()).limit(limit).offset(offset)
        return list(self.session.scalars(stmt))

    def simulations(self, report_id: str) -> list[SimulationRun]:
        self.get(report_id)
        return list(
            self.session.scalars(
                select(SimulationRun)
                .where(SimulationRun.report_id == report_id)
                .order_by(SimulationRun.scenario_index, SimulationRun.mode)
            )
        )

    def trades(
        self,
        report_id: str,
        scenario_index: int | None = None,
        mode: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[tuple[SimTrade, SimulationRun]]:
        self.get(report_id)
        stmt = (
            select(SimTrade, SimulationRun)
            .join(SimulationRun, SimulationRun.id == SimTrade.simulation_run_id)
            .where(SimTrade.report_id == report_id)
        )
        if scenario_index is not None:
            stmt = stmt.where(SimulationRun.scenario_index == scenario_index)
        if mode:
            stmt = stmt.where(SimulationRun.mode == mode)
        stmt = stmt.order_by(SimulationRun.scenario_index, SimulationRun.mode, SimTrade.seq).offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        return [(t, r) for t, r in self.session.execute(stmt).all()]

    def latest_reviews(self, sim_trade_ids: list[int]) -> dict[int, TradeReview]:
        if not sim_trade_ids:
            return {}
        rows = self.session.scalars(
            select(TradeReview).where(TradeReview.sim_trade_id.in_(sim_trade_ids)).order_by(TradeReview.id)
        )
        return {r.sim_trade_id: r for r in rows}
