# Original tier sizing and corrected risk sensitivity

The original README confirms the documented distinction: standard momentum 3 lots 1-OTM; super-trend 4 lots 2-OTM; scalp/countertrend 2 lots ATM. Earlier source inspection limited to the risk agent missed this documented design.

Documentation and implementation differ. The archived resolver chooses 2-OTM for all setups (3 standard, 4 super-trend, 2 wide-stop), with a default 7.5% risk budget (3,750 on 50,000) and premium/margin budget 28,000. Its minimum-one-lot floor can bypass a computed zero affordability count. The frozen old risk agent also uses fixed3/4 and assumed delta0.5; the old27 rows do not establish an executed ATM/OTM tier mix. This study is not an exact replay of the original engine. Evidence hashes: archive-evidence.json.

## Corrected allocation experiment

Independent 50,000 accounts, stop budget 2,500 or 3,000 (reduced proportionally when cash falls below50,000), ATM/ITM cap2 lots, OTM cap4. Largest eligible whole-lot size; current5,000 full-premium and daily-exposure limits retained, so not all accounts use their full risk budget or lot cap. One position, same expanded signals, same20% net-profit giveback after1R, stop and15:10 square-off. No dynamic conviction classification; OTM4 is a ceiling rather than an automatic super-trend classification.

## Recorded-book late-session shadows

| Signal time | ITM lots / net | ATM lots / net | OTM lots / net |
|---|---:|---:|---:|
| 13:38 | 1 / 2523.64 | 1 / 2198.10 | 4 / 1569.13 |
| 13:52 | 1 / 951.23 | 2 / 1484.93 | 4 / -1427.24 |

Both2,500 and3,000 ceilings select the same recorded-example sizes and produce the same outcomes. At13:38 one-lot stop estimates were ITM1,415.30, ATM1,029.92; four-lot OTM1,337.59. At13:52: ITM1lot1,001.48; ATM2lots1,413.47; OTM4lots859.61. The shared ceiling is not identical deployed risk.

These are independent alternative shadows using recorded fresh bid/ask and verified daily costs, not actual executed trades at these new sizes. Roughly11-second quotes miss one-second peaks. Separate flat accounts/no preceding daily loss at each signal. The13:38 path overlaps13:52, so do not sum these as portfolio profit. Morning entry-time option quotes are unavailable.

## Synthetic expiry-session portfolio comparison

Four expiry sessions before today:19Oct,27Oct,3Nov,9Nov. Net after assumed costs:

| Policy | Trades at2,500 ceiling | Net at2,500 | Trades at3,000 ceiling | Net at3,000 |
|---|---:|---:|---:|---:|
| OTM | 44 | 15112.66 | 44 | 17925.28 |
| ATM | 44 | 12398.41 | 44 | 12398.41 |
| ITM | 26 | 9324.86 | 26 | 9324.86 |
| TIME_ROTATION | 45 | 7746.80 | 45 | 7746.80 |

TIME_ROTATION is ITM before11:00, ATM11:00-13:30, OTM after13:30; no risk fallback. Full five-session values, including today’s older-VIX assumption, and time buckets are in accounts/report.md.

## Validation and limitations

All eight model accounts reconcile independently: fees, cash, risk limits, whole-lot quantities, one position, full-premium and daily exposure limits, and marked drawdown. Six focused research-limit tests passed; existing default risk limits were restored after the test process and never changed in the running paper process. Eligible recorded-path P&L and lot/risk ceilings reconcile independently.

Historical books/IV surface/news are absent. Black76/VIX synthetic prices previously differed materially from actual same-day quotes, so model rankings are conditional and cannot prove executable profits. Only four past expiry sessions are available; the rules were motivated by today, so this is not independent forward validation. Today’s model uses13Nov VIX because16Nov VIX is missing. Higher risk/lot ceilings change eligibility, exits, capacity and losses; this is a sensitivity study, not a recommendation to raise risk. The original28,000 premium budget was not restored.

The user’s tier-sizing distinction is supported by original documentation. The corrected recorded examples show ITM outperforming at13:38 and ATM at13:52; this does not establish universal expiry-day ITM superiority. Active paper policy remains estimated stop budget1,000, maximum3lots, full-premium/daily caps5,000; no Telegram or live-order changes.
