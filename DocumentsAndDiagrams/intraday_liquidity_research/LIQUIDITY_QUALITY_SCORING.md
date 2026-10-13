# Liquidity quality scoring

Data: Kaggle NIFTY 1-minute bars rolled to 5m, 2015-01-02 to 2025-12-24 (2295 sessions; the first session and NIFTY expiry days are skipped for every strategy). VWAP: session time-weighted typical price (index has no volume). In-sample to 2020, out-of-sample 2021 on. Costs: 2 index pts a round trip. Results are index points and R; option P&L is not reported because no historical option prices are available.

Pool: the first baseline trade of each liquidity event across the intraday strategies (2140 trades), so one price move counts once. Each feature is split into in-sample terciles (or its values). No weights are assumed.

## Per-feature R

| Feature | Group | IS trades | IS avg R | OOS trades | OOS avg R | IS best/worst | Holds OOS |
|---|---|---|---|---|---|---|---|
| tf | high >=60 | 282 | -0.027 | 414 | -0.009 | worst |  |
| tf | mid <60 | 502 | -0.001 | 942 | 0.035 | best | yes |
| external | 0 | 767 | -0.005 | 1325 | 0.023 | best | yes |
| external | 1 | 17 | -0.248 | 31 | -0.053 | worst |  |
| age_min | high >=15 | 335 | -0.003 | 486 | 0.011 | best | yes |
| age_min | low <5 | 246 | -0.02 | 488 | -0.003 | worst |  |
| age_min | mid <15 | 203 | -0.012 | 382 | 0.066 |  |  |
| depth_atr | high >=1.016 | 263 | -0.076 | 339 | 0.037 | worst |  |
| depth_atr | low <0.509 | 261 | 0.012 | 564 | 0.003 |  |  |
| depth_atr | mid <1.016 | 260 | 0.032 | 453 | 0.033 | best | no |
| reclaim_atr | high >=2.84 | 262 | 0.014 | 296 | 0.086 |  |  |
| reclaim_atr | low <2.038 | 261 | -0.096 | 535 | 0.038 | worst |  |
| reclaim_atr | mid <2.84 | 261 | 0.05 | 525 | -0.031 | best | no |
| displacement_atr | high >=1.299 | 262 | 0.05 | 380 | 0.026 | best | yes |
| displacement_atr | low <0.781 | 261 | -0.009 | 527 | 0.03 |  |  |
| displacement_atr | mid <1.299 | 261 | -0.073 | 449 | 0.008 | worst |  |
| vwap_atr | high >=1.679 | 262 | 0.085 | 464 | 0.088 | best | yes |
| vwap_atr | low <0.821 | 261 | -0.082 | 440 | -0 | worst |  |
| vwap_atr | mid <1.679 | 261 | -0.036 | 452 | -0.026 |  |  |
| room_r | high >=0.62 | 239 | 0.051 | 349 | 0.017 | best | no |
| room_r | low <0.26 | 235 | -0.09 | 448 | 0.045 | worst |  |
| room_r | mid <0.62 | 239 | -0.022 | 414 | 0.011 |  |  |
| with_open | 0 | 301 | -0.068 | 464 | -0.039 | worst |  |
| with_open | 1 | 483 | 0.025 | 892 | 0.053 | best | yes |
| bars_to_entry | high >=9 | 327 | 0.031 | 496 | -0.015 | best | no |
| bars_to_entry | low <6 | 206 | -0.11 | 450 | 0.027 | worst |  |
| bars_to_entry | mid <9 | 251 | 0.016 | 410 | 0.06 |  |  |
| vol_ratio | high >=1.385 | 251 | -0.002 | 345 | -0.017 | worst |  |
| vol_ratio | low <1.099 | 246 | 0.004 | 635 | 0.064 | best | yes |
| vol_ratio | mid <1.385 | 251 | 0.001 | 376 | -0.015 |  |  |
| hour | high >=13 | 322 | 0.05 | 438 | -0.028 | best | no |
| hour | low <11 | 186 | -0.124 | 326 | 0.053 | worst |  |
| hour | mid <13 | 276 | -0.006 | 592 | 0.041 |  |  |

## Score
Features chosen in-sample (best vs worst group 0.1R+ apart, 30+ trades each): depth_atr, reclaim_atr, displacement_atr, vwap_atr, room_r, bars_to_entry, hour. Score = how many of them a trade has in its best in-sample group. The out-of-sample columns are the test.

| Score | IS trades | IS avg R | OOS trades | OOS avg R |
|---|---|---|---|---|
| 0 | 27 | 0.126 | 76 | 0.063 |
| 1 | 158 | -0.198 | 303 | -0.037 |
| 2 | 229 | -0.053 | 428 | 0.114 |
| 3 | 215 | -0.001 | 325 | -0.016 |
| 4 | 111 | 0.125 | 163 | -0.035 |
| 5 | 36 | 0.336 | 48 | -0.021 |
| 6 | 8 | 0.735 | 12 | -0.102 |
| 7 | 0 |  | 1 | -0.051 |
