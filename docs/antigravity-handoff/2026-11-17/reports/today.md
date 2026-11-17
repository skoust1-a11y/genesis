# 17 November: actual paper and research accounts

Actual operational candidate: one closed 13:52 25550 CE trade, three lots, net -1,082.46 after costs. Existing 1,000 stop-risk policy; earlier signals were missed during Mac sleep.

## Full-session synthetic account comparison

Counterfactual uninterrupted operation with expanded entries; starting capital50,000 (accounts carried prior expiry-session results), 2,500/3,000 stop-risk ceilings, ATM/ITM cap2, OTM cap4, premium/daily caps5,000. Today produces identical trade lists/net under both ceilings. No real morning option books; synthetic Black76/VIX prices with assumed fees/news.

| Policy | Trades | Modeled net after costs | Today marked drawdown |
|---|---:|---:|---:|
| OTM | 8 | 4191.62 | 2578.65 |
| ATM | 8 | -3371.53 | 3371.53 |
| ITM | 5 | 745.36 | 2411.12 |
| Time rotation | 6 | 7206.24 | 2586.65 |

## Time-rotation trades

ITM entries before11:00; ATM11:00-13:30; OTM thereafter. Selection applies only to new entries, not automatic rolling of existing holdings. Today no new late-OTM trade was admitted in this lane.

| Entry IST | Exit IST | Side | Selection | Lots | Modeled net |
|---|---|---|---|---:|---:|
| 09:28:05 | 09:34:05 | CALL | ITM | 1 | -1019.33 |
| 09:55:05 | 11:14:05 | CALL | ITM | 1 | 1637.41 |
| 11:17:05 | 12:04:05 | CALL | ATM | 2 | 2798.18 |
| 12:08:05 | 12:37:05 | CALL | ATM | 2 | -1481.28 |
| 12:37:05 | 13:13:05 | PUT | ATM | 1 | 3630.62 |
| 13:14:05 | 13:46:05 | CALL | ATM | 2 | 1640.64 |

## Separate recorded-book shadows

| Signal IST | ITM | ATM | OTM |
|---|---:|---:|---:|
| 13:38 | +2,523.64 (1 lot) | +2,198.10 (1 lot) | +1,569.13 (4 lots) |
| 13:52 | +951.23 (1 lot) | +1,484.93 (2 lots) | -1,427.24 (4 lots) |

These are independently funded hypothetical alternatives; cannot be added to each other, to the model, or to actual P&L. The actual 13:52 OTM trade had three lots and lost1,082.46; four-lot shadow is a separate risk configuration. Roughly11-second books miss one-second peaks.

The 09:39 reversal remains vulnerable to an already open opposite trade. Midday/afternoon movements may be covered by earlier positions without a separate exact-time re-entry. More signals did not eliminate availability, risk-capacity or wrong-side entry issues.

Today’s time rotation is the highest-net tested model lane, not an independently validated best policy: it trailed OTM across the four earlier expiry sessions. November16 VIX is absent, so today’s model carries November13 VIX. A nearby13:38 real book was8.70/8.75 versus a model0.95/1.00, demonstrating material model admission/P&L uncertainty. No verified whole-day executable options P&L can be supplied from missing morning books. Active operational policy unchanged.
