"""Dynamic strike selection and fair risk/lot comparison research.

Compares ATM, shallow ITM, and OTM options under identical monetary risk ceilings:
- Strict whole-lot sizing (no fractional lots).
- No forced minimum lot: if 1 lot exceeds premium or risk caps, trade is rejected.
- Evaluates:
  1. Fixed OTM (Control - Step +2).
  2. Fixed ATM (Step 0).
  3. Fixed shallow ITM (Step -1 or -2).
  4. Time-of-day rotation on expiry days (<11:00 ITM, 11:00-13:30 ATM, >13:30 OTM).
  5. Ranked fallback: declared preference order with fallback when preferred is rejected.
- Detailed audit logs capturing all attempted strikes and concrete rejection reasons.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any, Literal

from app.config import Settings
from app.trading import paper_fees, paper_risk
from app.trading.option_quote import IST, ContractQuote

VERSION = "strike-selection-research-v1"


@dataclass
class StrikeAttempt:
    step: int
    strike: float
    tier: Literal["ITM", "ATM", "OTM"]
    delta: Decimal | None
    bid: Decimal | None
    ask: Decimal | None
    spread_pct: Decimal | None
    allowed_lots: int | None
    estimated_stop_risk: Decimal | None
    full_premium_loss: Decimal | None
    is_eligible: bool
    rejection_reason: str | None


@dataclass
class SelectionResult:
    chosen_step: int | None
    chosen_strike: float | None
    chosen_tier: Literal["ITM", "ATM", "OTM"] | None
    chosen_quote: ContractQuote | None
    sizing_decision: paper_risk.SizingDecision | None
    attempts: list[StrikeAttempt]
    policy_name: str
    rejection_reason: str | None = None


def tier_from_step(step: int) -> Literal["ITM", "ATM", "OTM"]:
    if step < 0:
        return "ITM"
    elif step == 0:
        return "ATM"
    return "OTM"


class StrikeSelector:
    """Pure strike selector with declared risk ceilings and sizing rules."""

    def __init__(
        self,
        stop_budget_ceiling: Decimal = Decimal("1000.00"),
        full_premium_limit: Decimal = Decimal("5000.00"),
        daily_exposure_limit: Decimal = Decimal("5000.00"),
        max_lots_atm: int = 2,
        max_lots_itm: int = 2,
        max_lots_otm: int = 3,
        settings: Settings | None = None,
    ):
        self.stop_budget_ceiling = stop_budget_ceiling
        self.full_premium_limit = full_premium_limit
        self.daily_exposure_limit = daily_exposure_limit
        self.max_lots_atm = max_lots_atm
        self.max_lots_itm = max_lots_itm
        self.max_lots_otm = max_lots_otm
        self.settings = settings or Settings()

    def select(
        self,
        *,
        policy: Literal["FIXED_OTM", "FIXED_ATM", "FIXED_ITM", "TIME_ROTATION", "RANKED_FALLBACK"],
        entry_spot: float,
        stop_spot: float,
        direction: Literal["CALL", "PUT"],
        now: datetime,
        chain: dict[str, Any],
        is_expiry_day: bool,
        cash: Decimal,
        gross_daily_loss: Decimal = Decimal("0.00"),
        existing_reservations: Decimal = Decimal("0.00"),
        fee_policy_id: str | None = None,
        quote_fetcher: Any | None = None,
        evidence_map: dict[str, paper_risk.DeltaEvidence] | None = None,
    ) -> SelectionResult:
        """Select strike and size whole lots according to chosen policy."""
        if now.utcoffset() is None:
            now = now.replace(tzinfo=IST)
        now_ist = now.astimezone(IST)
        time_str = now_ist.strftime("%H:%M")
        kind: Literal["CE", "PE"] = "CE" if direction == "CALL" else "PE"
        sign = 1 if direction == "CALL" else -1

        # Stop budget calculation: min(ceiling, 2% cash)
        stop_budget = min(self.stop_budget_ceiling, (cash * Decimal("0.02")).quantize(Decimal("0.01"), rounding=ROUND_DOWN))

        # Determine target step(s) based on policy
        target_steps: list[int]
        if policy == "FIXED_OTM":
            target_steps = [2]
        elif policy == "FIXED_ATM":
            target_steps = [0]
        elif policy == "FIXED_ITM":
            target_steps = [-1]
        elif policy == "TIME_ROTATION":
            if is_expiry_day:
                if time_str < "11:00":
                    target_steps = [-1]  # ITM in morning
                elif time_str < "13:30":
                    target_steps = [0]   # ATM midday
                else:
                    target_steps = [2]   # OTM afternoon
            else:
                target_steps = [2]       # Retain OTM on non-expiry days
        elif policy == "RANKED_FALLBACK":
            # Declare preference order: ITM -> ATM -> OTM
            target_steps = [-1, 0, 2]
        else:
            raise ValueError(f"Unknown strike selection policy: {policy}")

        attempts: list[StrikeAttempt] = []
        passing: list[tuple[int, float, Literal["ITM", "ATM", "OTM"], ContractQuote, paper_risk.SizingDecision]] = []

        # Available strike steps to evaluate
        all_candidate_steps = [-2, -1, 0, 1, 2, 3, 4] if policy == "RANKED_FALLBACK" else target_steps

        for step in all_candidate_steps:
            tier = tier_from_step(step)
            strike = float(round(entry_spot / 50.0) * 50.0 + sign * step * 50.0)

            # Get quote from chain or fetcher
            q: ContractQuote | None = None
            quote_why: str | None = None
            if quote_fetcher is not None:
                q, quote_why = quote_fetcher(chain, strike=strike, option_type=kind, now=now_ist)
            else:
                # Built-in basic lookup from chain dictionary if quote_fetcher not provided
                q = None
                quote_why = "NO_QUOTE_FETCHER_PROVIDED"

            if q is None:
                attempts.append(StrikeAttempt(
                    step=step, strike=strike, tier=tier, delta=None, bid=None, ask=None,
                    spread_pct=None, allowed_lots=0, estimated_stop_risk=None,
                    full_premium_loss=None, is_eligible=False, rejection_reason=quote_why or "QUOTE_UNAVAILABLE"
                ))
                continue

            # Check spread
            spread_pct = ((q.ask - q.bid) / ((q.ask + q.bid) / Decimal("2.0"))) * Decimal("100.0")
            evidence = evidence_map.get(q.tradingsymbol) if evidence_map else None

            # Determine max lot cap for this tier
            if tier == "ATM":
                tier_max_lots = self.max_lots_atm
            elif tier == "ITM":
                tier_max_lots = self.max_lots_itm
            else:
                tier_max_lots = self.max_lots_otm

            # Call sizing engine
            prev_max_lots = paper_risk.MAX_LOTS
            paper_risk.MAX_LOTS = tier_max_lots
            try:
                sized = paper_risk.size(
                    quote=q,
                    evidence=evidence,
                    trusted_sources=frozenset({evidence.source}) if evidence else frozenset({"upstox"}),
                    now=now_ist,
                    settings=self.settings,
                    underlying_entry=Decimal(str(entry_spot)),
                    underlying_stop=Decimal(str(stop_spot)),
                    stop_budget=stop_budget,
                    gross_daily_loss=gross_daily_loss,
                    existing_reservations=existing_reservations,
                    available_cash=cash,
                    cost_policy_id=fee_policy_id or paper_fees.POLICY_ID,
                )
            except (ValueError, TypeError, KeyError) as e:
                sized = paper_risk.SizingDecision(None, None, None, f"SIZING_ERROR: {e}")
            finally:
                paper_risk.MAX_LOTS = prev_max_lots

            is_ok = sized.entry is not None
            attempts.append(StrikeAttempt(
                step=step,
                strike=strike,
                tier=tier,
                delta=evidence.value if evidence else None,
                bid=q.bid,
                ask=q.ask,
                spread_pct=spread_pct.quantize(Decimal("0.01")),
                allowed_lots=sized.entry.lots if sized.entry else 0,
                estimated_stop_risk=sized.estimated_stop_risk,
                full_premium_loss=sized.full_premium_loss,
                is_eligible=is_ok,
                rejection_reason=sized.reason if not is_ok else None,
            ))

            if is_ok:
                passing.append((step, strike, tier, q, sized))

        # Select winning strike based on policy preference order
        if not passing:
            return SelectionResult(
                chosen_step=None,
                chosen_strike=None,
                chosen_tier=None,
                chosen_quote=None,
                sizing_decision=None,
                attempts=attempts,
                policy_name=policy,
                rejection_reason=attempts[0].rejection_reason if attempts else "NO_ATTEMPTS",
            )

        # For strict policies, find the passing attempt matching target_steps
        chosen: tuple[int, float, Literal["ITM", "ATM", "OTM"], ContractQuote, paper_risk.SizingDecision] | None = None
        for t_step in target_steps:
            for cand in passing:
                if cand[0] == t_step:
                    chosen = cand
                    break
            if chosen is not None:
                break

        # If ranked fallback and target step not available, take best passing
        if chosen is None and policy == "RANKED_FALLBACK":
            chosen = passing[0]

        if chosen is None:
            return SelectionResult(
                chosen_step=None,
                chosen_strike=None,
                chosen_tier=None,
                chosen_quote=None,
                sizing_decision=None,
                attempts=attempts,
                policy_name=policy,
                rejection_reason="TARGET_TIER_NOT_ELIGIBLE",
            )

        step, strike, tier, q, sized = chosen
        return SelectionResult(
            chosen_step=step,
            chosen_strike=strike,
            chosen_tier=tier,
            chosen_quote=q,
            sizing_decision=sized,
            attempts=attempts,
            policy_name=policy,
            rejection_reason=None,
        )
