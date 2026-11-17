# Prompt 03 — Completed1m morning entries and re-entry quality

The owner wants primary opportunities after09:30, particularly09:30–10:30 with selective extension to11:00. Treat that as a testable hypothesis, not a universal Indian-market fact. Use completed1m entry candles and explicitly separate context timeframe from trigger timeframe.

Review the preserved `morning_policy.py`, tests, signal schedules and report. The9:37 candle's close is actionable at09:38. It sweeps a previously confirmed LOCAL low; the session low was lower. A rule requiring the session/opening low sweep would miss this example. Never use future swing confirmation or fill at a historic candle price.

Test opening-range09:15–09:30 context and confirmed local swings as optional, separately labeled directional filters. Investigate the09:34 PUT that blocks09:38 CALL: define a generic directional/failed-breakout rule, rather than excluding that timestamp after seeing the result. Any opposite-position exit/reversal must be a separate management variant with fees, fresh quotes and one-position reconciliation. New entries never bypass pending exits.

Separate observed signal IDs, admission outcomes and executed trade state. A stale rejected signal must not permanently consume the price zone. Conversely, do not retry one stale idea endlessly; require a fresh distinct pullback/trigger, invalidate old pending setups and deduplicate per setup. Allow a valid fresh re-entry while flat; do not require re-sweeping an original zone for every continuation. While already positioned in the same direction, classify continuation as holding evidence unless pyramiding is separately authorized (it is not currently).

Compare:09:30–10:30;09:30–11:00; morning plus separate afternoon12:00–15:00; original control. Retain signals rejected by quote/risk/position/time gates with reasons. Entry window must not impose an automatic exit at its end. Avoid changing entry, exit, selection and risk in one unlabeled experiment.

Validate across all available sessions, then reserved/forward sessions. Report missed-opportunity cases with what was knowable, which concrete gate blocked entry and whether another position already covered the move. Do not label every100point chart move a tradable signal or invent its option P&L.
