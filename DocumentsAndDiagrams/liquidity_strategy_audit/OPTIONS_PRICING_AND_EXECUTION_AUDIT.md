# Options pricing and execution audit

**There are no historical intraday NIFTY option prices in this repository.** Every option P&L figure below and in the 10-year workbooks is **modelled** from the index path with Black-76. The only real option prices available are NSE end-of-day closes (2020 onward), used here to measure how wrong the model is. No premiums were invented.

## Implementation

| Piece | Code | Behaviour |
|---|---|---|
| Pricing | `option_pnl.black76` L22 | Undiscounted Black-76 (r = 0) on a forward |
| Live, chain available | `option_plan` L221, then `plan_pnl` (`option_pnl.py` L56) | Groww chain: quote, IV implied from the quote, forward from put-call parity (`implied_basis` L45) |
| Live fallback | `estimated_plan` L249-267 | India VIX as IV for every strike, **forward = spot**, exit premiums priced 0.15 day after entry |
| Backtest | `backtest_liquidity.backtest` L208, `option_exit` L191 | Same fallback: previous-day VIX, forward = spot, IV constant to the exit |
| Strike | `strike_of` L59-61 | ATM in the specification; working tree is 2 strikes OTM |
| Expiry | `weekly_expiry` L241 (live), `expiry_for` L163 (backtest) | Nearest weekly or monthly expiry (see calendar below) |

## Black-76 vs real NSE closes (1,318 days, nearest-expiry ATM)

Model: Black-76 at the **previous day's** India VIX, forward = spot, priced at the same day's close (as the backtest does).

| Type | DTE | Days | Median error | Mean abs error | P90 abs error | Contract IV / VIX |
|---|---|---|---|---|---|---|
| CE | 1 | 333 | -5.3% | 18.6% | 34.5% | 1.01 |
| CE | 2-3 | 551 | -3.3% | 13.7% | 28.9% | 0.98 |
| CE | 4-7 | 434 | +10.3% | 16.4% | 33.2% | 0.85 |
| PE | 1 | 333 | +1.2% | 18.0% | 36.5% | 1.01 |
| PE | 2-3 | 551 | +3.6% | 17.2% | 38.5% | 0.98 |
| PE | 4-7 | 434 | +28.2% | 33.1% | 64.7% | 0.85 |

Findings (measured):

- At 4-7 DTE the model **overprices** premiums: weekly IV is about 0.85 x VIX.
- PE overpricing at 4-7 DTE (+28%) is mostly forward = spot. The put-call parity forward sits a median **8.5 pts above spot** (P10 -12.9, P90 +37.9), so puts are dearer and calls cheaper than the model says. With the parity forward, the PE error falls to +19.8%.
- Skew: the 2% OTM put trades about **+3.7 vol pts** over ATM; the 2% OTM call about +0.45. The single-IV model underprices OTM puts.
- A single-trade P90 error of 29-65% means any one alert's "~Rs" premium can be far off. The live alert already says "check the real premium" when it falls back to the model (L169).

## Option-level R (modelled, 544 baseline trades, 1 lot)

Option R is option P&L after charges and slippage, divided by the option loss at the index stop.

| Model | Mean option R | CI95 |
|---|---|---|
| Index R, gross (reference) | +0.099 | [0.019, 0.181] |
| ATM, slippage Rs 0 per side | +0.123 | [0.037, 0.212] |
| **ATM, slippage Rs 0.5 per side (base)** | **+0.087** | **[0.003, 0.173]** |
| ATM, slippage Rs 1 | +0.053 | [-0.029, 0.136] |
| ATM, slippage Rs 2 | -0.008 | [-0.085, 0.070] |
| ATM, IV x0.9 at exit | **-0.236** | - |
| ATM, IV x1.1 at exit | **+0.411** | - |
| 2 OTM (working tree), slip 0.5 | +0.087 | [-0.004, 0.181] |
| v3 exits (3R + BE), ATM | +0.120 | [0.030, 0.213] |
| v3 exits, 2 OTM | +0.127 | [0.028, 0.232] |

- At the 2R target an ATM option earns about **2.11 option R** (range 1.52-3.75). At the stop it loses about -0.95 (range -1.02 to -0.81). Gamma and the short hold make the option payoff slightly convex relative to the index.
- **IV is the dominant risk.** A 10% IV move between entry and exit (for example the post-event IV crush, or the intraday IV drift on expiry-adjacent days) shifts expectancy by about 0.3R per trade, roughly 3x the strategy's entire edge. The constant-IV assumption is the weakest link in every rupee figure in the workbooks.
- Contract-IV check (2020 onward, 374 trades, previous-day IV of the actual traded strike and expiry plus the parity forward): mean option R +0.064 vs +0.047 with the VIX model on the same trades. Entry premiums median 0.91 x the VIX model. The direction of the VIX model's bias is conservative on premium but does not change the conclusion.
- The base option result (+0.087, CI just above 0) depends on an assumed Rs 0.5 slippage, constant IV and EOD-calibrated IV. It is **not** a measured edge.

## Expiry calendar

| Period | NIFTY expiry | Code |
|---|---|---|
| to 2019-02-10 | Monthly (last Thursday) | `expiry_for` |
| 2019-02-11 to 2025-08-31 | Weekly Thursday | `expiry_for` |
| from 2025-09-01 | Weekly Tuesday | `expiry_for`, `weekly_expiry` |

`expiry_for` disagrees with the NSE-listed nearest expiry on 8 days (2023-06-23..27 and 2025-10-15..21, `RESULTS.md` Options). Before 2019 the expiry filter removes about 12 days a year instead of about 50, so the 2016-2018 sample keeps about 38 Thursdays a year that later years skip.

## Execution realism

- **Fill price:** the alert gives the index close. The option is bought after the alert arrives (a few minutes of Yahoo delay plus human reaction). Model slippage of Rs 0.5 per side is optimistic for fast markets. Rs 2 per side removes the edge entirely.
- **Liquidity:** ATM and near-OTM weekly NIFTY options are very liquid. Size is not a constraint at retail size. Not measured (no order-book data).
- **Exit:** stop and target are on the **index**, so the option is exited by watching the index. The alert lists approximate premiums only.
- **Charges:** NSE/SEBI/STT/GST/brokerage via `app.paper.order_charges` (dated rates).
- **Lot sizes:** dated table `data/lot_sizes/nse.yaml` (75, then 50, 25, 75, 65).

## Position sizing (Part 5)

Modelled option P&L, whole lots, compounding, 2016-2026. Full table in `RISK_AND_DRAWDOWN_ANALYSIS.md`.

| Capital | Risk per trade | Trades taken | CAGR | Max DD |
|---|---|---|---|---|
| Rs 1 lakh | 0.5% | 0 of 544 (one lot always exceeds the budget) | 0 | 0 |
| Rs 1 lakh | 1% | 21 | -0.6% | 6.9% |
| Rs 1 lakh | 2% | 371 | 3.7% | 20.4% |
| Rs 1 lakh | 5% (current rule) | 542 | 14.5% | **57.1%** |
| Rs 5 lakh | 1% | 534 | 3.2% | 14.6% |
| Rs 5 lakh | 2% | 544 | 7.5% | 28.3% |
| Rs 5 lakh | 5% | 544 | 16.6% | **58.3%** |

- With small capital, whole-lot sizing at low risk percentages skips most trades. The result is then a function of **which** trades fit, not of the strategy.
- The 5% rule produces a 57-58% modelled drawdown and a 65% chance of a 50% drawdown (risk-of-ruin bootstrap). It is not defensible for a strategy whose edge has not been demonstrated. Recommendation: at most 1% (preferably 0.5%) of capital at risk, and only in paper trading until the forward test passes.
