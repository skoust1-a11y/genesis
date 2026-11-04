# Recency intelligence architecture

Date: 5 Nov 2026. Design only. Not implemented. Shadow mode only. No order path. The PDC Zone + 15 EMA V0 entry, stop, target, window, and re-entry rules are not inputs this layer may edit.

Financial integrity and this layer are separate. Recency may rank, explain, and recommend WAIT or NO TRADE in a shadow record. It must not change a strategy condition, relax a stop, size a position, or override `RiskAgent.evaluate_order`. A recency failure leaves the existing decision unchanged.

## Where it sits

Existing code this design extends. No new agent framework. No second market-data pipeline. No second order manager.

| Piece | File and symbol | Role in Sprint 2 |
|---|---|---|
| Bars, VWAP, swings | `TradeManager.on_market_tick` already receives `df_5m` and `df_15m`. `compute_session_vwap` lives in `holding_agent.py`. | Read-only inputs to price-action evidence. |
| Headlines | `app.market.news` and `NewsAgent.analyze_headlines` | Event intake. The keyword score stays. It is not a validated weight. |
| Calendar | `app.market.calendar.load_event_yaml` and `NewsAgent.SCHEDULED_HIGH_IMPACT_EVENTS` | Scheduled events. Do not change `check_event_risk_guard` or the detector blackout in this sprint. That blackout is existing behaviour. Recency records it. It does not add a new veto. |
| Open positions | `HoldingAgent.evaluate_holding` | After it returns, a shadow classifier records thesis state. It does not replace the action. |
| Decisions | `TradeManager.on_market_tick` | After the manager has already chosen an action, append a shadow record. Do not branch on it. |
| New module | `backend/app/trading/crew/recency.py` | Pure functions: decay, evidence, conviction, entry assessment, position assessment. No I/O and no orders. |
| Shadow log | `trades/recency_shadow.jsonl` | Append-only. A write failure is logged and ignored by the trade path. |

`NewsAgent` is not constructed by `TradeManager` today. The scanner crew owns it. The shadow hook reads whatever headline list the scanner already cached. It does not open a second RSS client.

## What recency is allowed to say

In shadow mode the record may contain:

- A rank among candidates the strategy already produced.
- Bullish, bearish, or neutral conviction as a band, not a fake percentage.
- Whether the evidence is fresh, conflicting, or missing.
- A hypothetical label: `WOULD_WAIT`, `WOULD_NO_TRADE`, or `NO_CHANGE`.

It must not:

- Change PDC Zone, 15 EMA, entry conditions, stop, target, or the trading window.
- Relax a stop or close a position.
- Bypass `RiskAgent`.
- Call `BrokerBridgeAgent.place_order`, `ExecutionManager`, or `trading/orders.py`.

Activation is a later gate (S2-V3). It is not part of building the shadow log. Even then, activation may only attach the hypothetical label as an additional veto in front of the journal, and only after the financial gate has already approved. It still cannot change the strategy's entry rules or the risk gate's arithmetic.

## D1 Price-action evidence

Each observation is one record. Age is `decision_time - bar_time`, using the bar's close time. A bar that has not closed is not visible. That is the look-ahead rule for prices.

Observations the functions may emit from bars the manager already holds:

- 5-minute and 15-minute close relative to the session VWAP.
- EMA direction and slope on the timeframe the strategy already computed. Recency reads the slope. It does not change the EMA length.
- Breakout or breakdown of a prior swing, and a failed breakout when price returns through the level on a later closed bar.
- Liquidity sweep tags already produced by the scanner. Recency does not redefine a sweep.
- Swing high and swing low, and a structure shift when a later closed bar takes out the swing that defined the prior leg.
- Volume expansion or contraction against the recent median of the same timeframe.
- Momentum acceleration or deterioration as the change in slope, not as a new indicator with a tuned threshold presented as fact.

Two ages are not equal. A 5-minute close from the last bar and a 5-minute close from two hours ago are different evidence. The decay function below is the baseline. It is a prior, not a fitted result.

## D2 Market-event evidence

Use only feeds and files the repo already has: `Settings.news_feeds`, `app.market.news`, YAML under `data/events/` via `load_event_yaml`, and the headline list `NewsAgent` already accepts. FII/DII, a geopolitical wire, or a corporate filing that is not in those inputs is absent. Absence is `insufficient_data` for that category. Do not scrape a new source in this design.

Each event record:

| Field | Rule |
|---|---|
| `event_timestamp` | When the event happened, if the source says so. |
| `first_observed_at` | When Genesis first stored it. Required. |
| `source` | Feed name or `data/events` path. |
| `source_reliability` | Prior in {low, medium, high}. A hand-maintained calendar file is high as a source of schedule, not as a source of surprise. An RSS headline is low until a source is calibrated. These words are not measured hit rates. |
| `category` | `breaking_news`, `scheduled_macro`, `global_index`, `flow`, `corporate`, `geopolitical`, `sector`. Unknown category is stored and scored as unknown, which drops directional use. |
| `directional_relevance` | `bullish`, `bearish`, `neutral`, or `unknown`. Keyword hits from `NewsAgent.BULLISH_KEYWORDS` and `BEARISH_KEYWORDS` may set a provisional direction. The keyword list is not a model. |
| `severity` | `low`, `medium`, `high`. Scheduled items already named in `SCHEDULED_HIGH_IMPACT_EVENTS` start as high. A headline with no other mark starts as low. |
| `lifecycle` | `upcoming`, `active`, `decaying`, `expired`. |
| `confidence` | Band, not a percentage. |
| `data_quality` | 0 when timestamp or source is missing. Otherwise the reliability band mapped only for ordering inside the shadow log. |

Dedup key: normalized title, source, and a time bucket of 15 minutes. A repeated poll of the same headline is one event. The second copy does not add conviction. A later headline that is a different title is a different event even when the words overlap; correlated-evidence handling down-weights it. It does not delete it.

## D3 Decay and the six factors

Baseline, for evidence that is allowed to decay:

\[
W(t) = e^{-\lambda t}
\]

\(t\) is age in minutes at the decision timestamp. \(t < 0\) is future information and is dropped. \(\lambda\) is per class. \(W\) is freshness only.

Half-life \(t_{1/2} = \ln 2 / \lambda\). The table is a research prior. It is not a validated weight. Calibration replaces the prior. It does not get a silent edit in the daemon.

| Class | Prior half-life | Why it is separate |
|---|---|---|
| Short-term price action (closed 5-minute) | 20 minutes | A stale 5-minute impulse should not match a new one. |
| Intraday liquidity event | 45 minutes | A sweep matters, then it is either structure or noise. |
| Medium-term structure (closed 15-minute swing) | 180 minutes | Structure outlives a single 5-minute bar. |
| Breaking news | 60 minutes | Headlines go stale inside a session. |
| Scheduled macro | Full weight inside the published window, then 120-minute half-life | The event is not "old" before it happens. Upcoming is a state, not a decay to zero. |
| Persistent structure (previous-day high or low, session open) | Floor plus slow decay | Age must not erase the level. |

Persistent structure:

\[
W_{struct}(t) = w_{floor} + (1 - w_{floor}) e^{-\lambda t}
\]

Prior \(w_{floor} = 0.5\), prior half-life 1 session. A previous-day high still counts as a level after lunch. The floor is a prior. Walk-forward either keeps it or moves it. Do not describe 0.5 as optimal.

Other shapes worth testing later, not used as the baseline: a step function for scheduled releases, and a linear decay for very short liquidity tags. Exponential is the baseline because it is monotone, has one parameter, and is easy to falsify. It is not assumed optimal.

Six factors, multiplied. Any missing factor is zero, not one.

1. Temporal freshness \(W(t)\).
2. Evidence reliability. Price printed from the daemon's own closed bars: prior high. RSS: prior low. Unknown: zero.
3. Directional relevance in {−1, 0, +1}. Unknown is 0.
4. Severity in {0.25, 0.5, 1} for low, medium, high. These three numbers are bins so the log can sort. They are not fitted coefficients.
5. Regime relevance. A trend-day tag does not score a range observation as if the study had proved it. Until regime labels exist from research, this factor is 1 for "same session" and 0 for "observation from a prior session", except persistent structure, which may cross the session.
6. Independence. Evidence that shares a `cluster_id` (same swing, same deduped story, same sweep) contributes once: the member with the largest absolute product of the other five factors. Further members of the cluster contribute 0 in the baseline. A softer down-weight is a later experiment, not the first ship.

No coefficient in this section is a statistically validated weight. `NewsAgent`'s −1 to +1 keyword score is a heuristic input to direction. It is not \(W(t)\).

### Calibration

Parameters move only by this loop, in research, not in the live file:

1. Freeze the strategy card and the cost model.
2. Pick one class half-life. Leave the others at the prior.
3. Walk forward. Fit on a past window. Evaluate on the next window the fit has not seen.
4. Require the change to improve net expectancy after `leg_costs`, and not only win rate. Maximum drawdown and profit factor are reported beside it.
5. Keep the prior if the out-of-sample change is inside noise or flips sign when the window moves.
6. Write the chosen prior into the shadow config with the window it was tested on. Do not write it into `active_strategy_params.json`.

Until that loop has a written result, the shadow log uses the priors above and the report says "prior, not calibrated".

## D4 Directional conviction

From the independent evidence visible at `decision_time`:

- Bullish mass = sum of positive products.
- Bearish mass = sum of absolute negative products.
- Conflict = the smaller of the two masses divided by the larger, or null when both are zero.
- Neutral is the result when both masses are near zero or data quality is insufficient. It is not a third invented score.
- Freshness is the freshness of the single strongest piece, plus a count of pieces newer than one half-life.
- Agreement is "one side holds at least twice the other, and at least two independent pieces". Otherwise the band is `conflicted` or `thin`.
- Data quality is `insufficient` when any required input timestamp is missing, the newest bar is older than its class half-life by a factor of four, or the function threw.
- Confidence bands: `low`, `medium`, `high`. Do not emit 73% or any other percentage. `high` requires agreement, data quality not insufficient, and the strongest piece inside one half-life. Otherwise `medium` or `low`.

The shadow text answers, in words:

- Which side the fresh evidence leans to.
- Which observations are inside one half-life.
- Which older observations fell below the structural floor's companions and no longer drive the mass.
- What would invalidate the lean: a closed bar through the swing that carries the lean, or a new independent piece on the other side with severity high.
- The alternative: the other side's mass, or "none visible".

## D5 Entry assessment

After `TradeManager` has already accepted or rejected a candidate with the existing rules and the rupee gate:

- Record whether fresh evidence agrees with the candidate direction.
- Record whether 15-minute structure agrees.
- Record whether a deduped event inside its window agrees, conflicts, or is absent.
- Record reliability and conflict.
- Set the hypothetical label. `WOULD_NO_TRADE` when data quality is insufficient is not allowed. Insufficient data sets `WOULD_WAIT` only as a hypothesis. The strategy decision is stored unchanged.

The hypothetical label is not read by `record_entry`.

## D6 Open-position monitoring

On each tick, after `HoldingAgent.evaluate_holding` returns, classify the thesis:

| Class | Meaning |
|---|---|
| `strengthening` | Fresh independent evidence agrees with the open direction and conflict is low. |
| `stable` | No new independent evidence inside one short half-life. Older structure still present. |
| `weakening` | Fresh evidence opposes, or momentum slope has deteriorated, and the opposing mass is material. |
| `invalidated` | A closed bar has broken the swing the thesis named, or a scheduled event in the `active` window contradicts it with severity high and reliability not low. |
| `insufficient_data` | Timestamps or bars missing. |

`invalidated` in the shadow log is not an exit. `ExitAgent` and the existing stop remain the only close paths. One new observation cannot close the position. A classification change is recorded. It does not call `record_exit`.

Thesis deterioration and stop-loss execution are different fields in the log. A trade can be `weakening` with the stop unhit, or stopped out while the last shadow class was `stable`.

## D7 Failure handling

| Failure | Behavior |
|---|---|
| Missing event or bar timestamp | That piece has freshness 0. It does not default to fresh. |
| Delayed bars | Age uses the bar time, not the arrival time. If the bar time is after `decision_time`, drop it. |
| Stale quote | Reference price older than the short half-life is not a fresh price-action piece. Estimated P&L may still display it as stale. Recency does not promote it. |
| Duplicate event | Ignored after the dedup key hits. |
| Conflicting news | Both pieces can enter. Independence clustering and the conflict band stop either one from being hidden. |
| Missing price observation | Price-action mass is empty. Class `insufficient_data` if nothing else is usable. |
| Wrong category | Unknown category contributes 0 directional mass. The raw event stays in the log. |
| Feed interruption | Last good evidence ages out. No carry-forward of a fresh flag. |
| Exception in `recency.py` | Catch at the `TradeManager` hook. Log `insufficient_data`. Do not fail the tick. Do not skip the risk gate. |

If freshness cannot be computed, conviction is `low` and data quality is `insufficient`. Never fill a missing age with \(W=1\).

## D8 Validation

Four configurations, same frozen strategy, same costs from `leg_costs`, same bars:

1. Strategy alone. Recency output discarded.
2. Strategy plus price-action recency as a shadow veto (`WOULD_NO_TRADE` and `WOULD_WAIT` remove the trade in the research book only).
3. Strategy plus event recency, same way.
4. Both.

Report for each, after costs: net expectancy, profit factor, maximum drawdown, win rate, average win, average loss, adverse excursion, favorable excursion, count of skipped trades, count of skipped trades that would have won, entry delay in bars, and return per unit of planned loss. A higher win rate with worse expectancy is not an acceptance.

Slices: trend days, range days, high-volatility days, sessions with a scheduled macro event, and at least two regimes that were not the fit window.

Controls:

- Decision time sees only closed bars and events with `first_observed_at <= decision_time`.
- An event timestamp in the future relative to the decision is excluded even if a later backfill wrote it.
- Dedup runs before scoring.
- Walk-forward and a final out-of-sample block.
- Half-life stability: the next-best half-life in the grid must not flip the sign of expectancy. If it does, the result is unstable and the prior stays.

The research harness is a script or a flag on the existing simulator. It is not the daemon. It does not write `daily_risk_config.json`.

## D9 Shadow record

One JSON object per decision, appended to `trades/recency_shadow.jsonl`:

- `prediction_timestamp` and the bar time it was allowed to see
- market state actually used (symbol, last closed 5-minute and 15-minute ids, reference price kind)
- conviction band, bullish mass, bearish mass, conflict, data quality
- evidence ids, ages, and cluster ids
- alternative thesis in one sentence
- subsequent outcome fields, filled later by a research job: excursion and whether the strategy's exit was a win on points. The live tick does not wait for the outcome.
- `strategy_action` exactly as `on_market_tick` already chose
- `hypothetical_action`
- `orders_sent: false`

A test that the hook calls `place_order` fails the sprint. The module does not import `BrokerBridgeAgent` or `ExecutionManager`.

Compare shadow hypotheticals with the frozen strategy on the outcome fields. Do not enable the hypothetical as a veto until that comparison is written down and explicitly approved. Approval is not a document this design can grant to itself.

## Regression tests the module needs

Named in the acceptance matrix as R-01 through R-12. They are pure-function tests plus one manager test that a shadow exception does not change the action list except for an added log failure field, and does not call a broker.
