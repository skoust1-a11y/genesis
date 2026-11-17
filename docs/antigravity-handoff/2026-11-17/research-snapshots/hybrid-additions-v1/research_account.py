"""Independent experimental paper account. No broker-order API is imported.

All money comes from fresh bid/ask fills using the existing V2 risk and fee
functions. Pending/unpriced exits retain exposure; restarting never clears them.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from app.brokers.upstox_stream import SOURCE
from app.db import paper_ledger
from app.market.hours import holidays, market_status
from app.trading import forward_zone_candidate as detector
from app.trading import paper_exec as px
from app.trading import paper_exit_policy, paper_profit_trail, paper_risk
from app.trading.market_validation import _start, session_frame
from app.trading.option_quote import IST

CAPITAL = Decimal("50000.00")


def encode(value):
    return json.dumps(value, sort_keys=True, default=str)


class CandidateAccount:
    def __init__(self, path, settings, *, run_id):
        if settings.live_trading_enabled or settings.sandbox_allow_live:
            raise ValueError("Paper comparison requires live trading disabled")
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.settings, self.run_id = settings, run_id
        self.profit_profile = paper_profit_trail.profile(settings.paper_candidate_profit_giveback_pct)
        self.profit_identity = paper_profit_trail.identity(settings.paper_candidate_profit_giveback_pct)
        if (settings.paper_candidate_profit_giveback_pct is not None
                and settings.paper_exit_quote_policy != paper_exit_policy.FRESH_BID):
            raise ValueError("Profit trailing requires the fresh-bid paper exit policy")
        self.lock = self.path.with_suffix(".lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.connect() as c:
                c.executescript("""
                CREATE TABLE IF NOT EXISTS meta (id INTEGER PRIMARY KEY CHECK(id=1), run_id TEXT NOT NULL,
                  rules TEXT NOT NULL, rules_hash TEXT NOT NULL, source_hash TEXT NOT NULL, capital TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS decisions (signal_id TEXT PRIMARY KEY, at TEXT NOT NULL,
                  outcome TEXT NOT NULL, reason TEXT NOT NULL, signal TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS trades (id TEXT PRIMARY KEY, day TEXT NOT NULL, body TEXT NOT NULL,
                  state TEXT NOT NULL CHECK(state IN ('OPEN','EXIT_PENDING','CLOSED')), exit_reason TEXT);
                CREATE TABLE IF NOT EXISTS fills (id INTEGER PRIMARY KEY, trade_id TEXT NOT NULL REFERENCES trades(id),
                  side TEXT NOT NULL CHECK(side IN ('BUY','SELL')), at TEXT NOT NULL, body TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_buy_per_trade ON fills(trade_id) WHERE side='BUY';
                CREATE TABLE IF NOT EXISTS outbox (key TEXT PRIMARY KEY, body TEXT NOT NULL,
                  state TEXT NOT NULL DEFAULT 'PENDING', result TEXT,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, actionable INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS cursors (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS profit_trails (trade_id TEXT PRIMARY KEY REFERENCES trades(id),
                  peak TEXT NOT NULL, peak_at TEXT NOT NULL, armed INTEGER NOT NULL,
                  threshold TEXT, marked_net TEXT NOT NULL, marked_at TEXT NOT NULL);
                """)
                source_hash = hashlib.sha256(
                    Path(detector.__file__).read_bytes() + Path(__file__).read_bytes()
                    + paper_exit_policy.identity(settings.paper_exit_quote_policy).encode()
                    + self.profit_identity.encode()
                ).hexdigest()
                identity = (
                    run_id,
                    encode(detector.RULES),
                    detector.RULE_HASH,
                    source_hash,
                    str(CAPITAL),
                )
                prior = c.execute(
                    "SELECT run_id,rules,rules_hash,source_hash,capital FROM meta WHERE id=1"
                ).fetchone()
                if prior and tuple(prior) != identity:
                    raise ValueError(
                        "Candidate rules/source/capital changed: create a new run instead of reusing this journal"
                    )
                c.execute("INSERT OR IGNORE INTO meta VALUES (1,?,?,?,?,?)", identity)
                paper_exit_policy.bind(c, settings.paper_exit_quote_policy, trades_table="trades")
                self._validate(c)
        except BaseException:
            self.lock.close()
            raise

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            with c:
                yield c
        finally:
            c.close()

    def close(self):
        self.lock.close()

    @classmethod
    def _validate(cls, c):
        if c.execute("SELECT count(*) FROM trades WHERE state<>'CLOSED'").fetchone()[0] > 1:
            raise ValueError("Multiple held positions")
        for row in c.execute("SELECT * FROM trades"):
            t=json.loads(row['body'])
            fills=[json.loads(f[0]) for f in c.execute("SELECT body FROM fills WHERE trade_id=? ORDER BY id",(row['id'],))]
            buys=[f for f in fills if f['side']=='BUY']
            sells=[f for f in fills if f['side']=='SELL']
            if len(buys)!=1: raise ValueError("Exactly one entry required")
            bought=buys[0]['qty']; sold=sum(f['qty'] for f in sells)
            if sold>bought or bool(sold==bought)!=(row['state']=='CLOSED'):
                raise ValueError("Fill/state quantity mismatch")
            remaining=t.get('remaining_lots', t['lots'] if not sells else 0)
            if row['state']!='CLOSED' and remaining*buys[0]['qty']//t['lots'] != bought-sold:
                raise ValueError("Remaining-lot quantity mismatch")
            for f in fills:
                if type(f['qty']) is not int or f['qty']<=0 or f['qty']%65 or Decimal(f['price'])<=0 or Decimal(f['charges_inr'])<0:
                    raise ValueError("Invalid fill")
        if cls._cash(c)<0: raise ValueError("Negative cash")

    @staticmethod
    def _cash(c):
        cash = CAPITAL
        for row in c.execute("SELECT body FROM fills ORDER BY id"):
            f = json.loads(row[0])
            value = Decimal(f["price"]) * f["qty"]
            fee = Decimal(f["charges_inr"])
            cash += -value - fee if f["side"] == "BUY" else value - fee
        return cash

    @staticmethod
    def _pnl(c, tid):
        total = Decimal(0)
        for row in c.execute(
            "SELECT body FROM fills WHERE trade_id=? ORDER BY id", (tid,)
        ):
            f = json.loads(row[0])
            value = Decimal(f["price"]) * f["qty"]
            fee = Decimal(f["charges_inr"])
            total += -value - fee if f["side"] == "BUY" else value - fee
        return total

    def _message(self, c, key, text):
        c.execute(
            "INSERT OR IGNORE INTO outbox(key,body,actionable) VALUES (?,?,?)",
            (
                key,
                f"GENESIS PAPER | CANDIDATE | {self.run_id}\n{text}\nExperimental {detector.VERSION}. No real broker order.",
                int(key.startswith("entry:")),
            ),
        )

    def _decision(self, c, signal, now, outcome, reason):
        c.execute(
            "INSERT OR IGNORE INTO decisions VALUES (?,?,?,?,?)",
            (signal["signal_id"], now.isoformat(), outcome, reason, encode(signal)),
        )

    def _quote(self, chain, *, strike, option_type, expiry, now):
        q = px.contract_quote(
            chain,
            strike=strike,
            option_type=option_type,
            expiry=expiry,
            underlying="NIFTY",
        )
        if q is None or not q.provider_instrument_id or not q.tradingsymbol:
            return None, "CONTRACT_IDENTITY_UNVERIFIED"
        if not paper_ledger.lot_verified("NIFTY", now.date().isoformat(), q.lot_size):
            return None, "LOT_MISMATCH"
        return q, None

    def _exit(self, c, trade_row, chain, now, spot, bar, fee_policy):
        t = json.loads(trade_row["body"])
        reason = trade_row["exit_reason"]
        if not reason:
            if now.strftime("%H:%M") >= "15:10" or t["day"] < now.date().isoformat():
                reason = (
                    "SQUARE_OFF"
                    if t["day"] == now.date().isoformat()
                    else "PRIOR_DAY_UNRESOLVED"
                )
            elif spot is not None:
                sign = 1 if t["side"] == "CALL" else -1
                if sign * (spot - t["stop_spot"]) <= 0:
                    reason = "STOP"
                elif self.settings.paper_candidate_profit_giveback_pct is None and sign * (spot - t["target_spot"]) >= 0:
                    reason = "TARGET"
                elif bar and datetime.fromisoformat(
                    bar["date"]
                ) >= datetime.fromisoformat(t["opened_at"]):
                    stop_touch = (
                        bar["low"] <= t["stop_spot"]
                        if sign == 1
                        else bar["high"] >= t["stop_spot"]
                    )
                    target_touch = self.settings.paper_candidate_profit_giveback_pct is None and (
                        bar["high"] >= t["target_spot"]
                        if sign == 1
                        else bar["low"] <= t["target_spot"]
                    )
                    if stop_touch or target_touch:
                        reason = "STOP_TOUCH" if stop_touch else "TARGET_TOUCH"
        trailing = self.settings.paper_candidate_profit_giveback_pct is not None
        if not reason and not trailing:
            return None
        q, why = self._quote(
            chain,
            strike=t["strike"],
            option_type=t["option_type"],
            expiry=t["expiry"],
            now=now,
        )
        if q is not None:
            entry = json.loads(c.execute("SELECT body FROM fills WHERE trade_id=? AND side='BUY'",
                                         (t["signal_id"],)).fetchone()[0])
            if (q.tradingsymbol != t["contract"] or q.provider_instrument_id != t["instrument_id"]
                    or q.lot_size * t["lots"] != entry["qty"]):
                q, why = None, "CONTRACT_IDENTITY_MISMATCH"
        q, quote_why = px.exit_executable(q, now, self.settings)
        why = why or quote_why
        if q is None or not fee_policy:
            why = why or "FEE_POLICY_UNVERIFIED"
            if not reason:
                return why
            c.execute("UPDATE trades SET state='EXIT_PENDING',exit_reason=? WHERE id=?", (reason, t["signal_id"]))
            self._message(
                c,
                f"exit-pending:{t['signal_id']}",
                f"EXIT PENDING {t['side']} {t['strike']} {t['option_type']}\n{reason}: {why}. "
                "Exposure is retained; new candidate entries are blocked until reconciliation.",
            )
            return why
        try:
            fill = px.fill(
                q,
                "SELL",
                t["lots"],
                now=now,
                settings=self.settings,
                ticks=paper_risk.SLIPPAGE_TICKS,
                cost_policy_id=fee_policy,
            )
        except ValueError as exc:
            if reason:
                c.execute("UPDATE trades SET state='EXIT_PENDING',exit_reason=? WHERE id=?", (reason, t["signal_id"]))
            return str(exc)
        trail_mark = None
        if trailing and q.quote_time >= datetime.fromisoformat(t["opened_at"]):
            current_net = self._pnl(c, t["signal_id"]) + fill.price * fill.qty - fill.charges_inr
            prior = c.execute("SELECT peak,peak_at FROM profit_trails WHERE trade_id=?", (t["signal_id"],)).fetchone()
            previous = Decimal(prior["peak"]) if prior else Decimal(0)
            trail_mark = paper_profit_trail.evaluate(previous_peak=previous, current_net=current_net,
                initial_risk=t["stop_risk"], percent=self.settings.paper_candidate_profit_giveback_pct)
            peak_at = now.isoformat() if not prior or trail_mark.peak > previous else prior["peak_at"]
            c.execute("INSERT OR REPLACE INTO profit_trails VALUES (?,?,?,?,?,?,?)",
                (t["signal_id"], str(trail_mark.peak), peak_at, int(trail_mark.armed),
                 str(trail_mark.threshold) if trail_mark.threshold is not None else None,
                 str(current_net), now.isoformat()))
            if not reason and trail_mark.triggered:
                reason = "PEAK_PROFIT_GIVEBACK"
        if not reason:
            return None
        c.execute("UPDATE trades SET state='EXIT_PENDING',exit_reason=? WHERE id=?", (reason, t["signal_id"]))
        c.execute(
            "INSERT INTO fills(trade_id,side,at,body) VALUES (?,?,?,?)",
            (t["signal_id"], "SELL", now.isoformat(), encode(asdict(fill))),
        )
        c.execute("UPDATE trades SET state='CLOSED' WHERE id=?", (t["signal_id"],))
        pnl = self._pnl(c, t["signal_id"])
        self._message(
            c,
            f"exit:{t['signal_id']}",
            f"EXIT {reason}\n{t['strike']} {t['option_type']} "
            f"| {t['lots']} lot(s) | simulated bid fill INR {fill.price}\nNet paper P&L INR {pnl}"
            + (f"\nPeak observed net INR {trail_mark.peak}; giveback {self.settings.paper_candidate_profit_giveback_pct}%"
               if trail_mark else ""),
        )
        return None

    def monitor_profit(self, *, now, chain_source, fee_policy):
        """Quote-only risk reduction; never detects or admits an entry or fetches candles."""
        if self.settings.paper_candidate_profit_giveback_pct is None:
            return []
        if now.utcoffset() is None:
            raise ValueError("Timezone-aware clock required")
        now = now.astimezone(IST)
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if paper_profit_trail.identity(self.settings.paper_candidate_profit_giveback_pct) != self.profit_identity:
                raise ValueError("Profit trail policy/source changed; create a new journal")
            self._validate(c)
            held = c.execute("SELECT * FROM trades WHERE state<>'CLOSED'").fetchall()
            if not held:
                return []
            page = chain_source().get("optionChain", {})
            chain = {"contracts": page.get("contracts", []), "fetched_at": now,
                     "expiry": (page.get("aggregatedDetails") or {}).get("currentExpiry") or page.get("expiry")}
            policy_id = fee_policy() if callable(fee_policy) else fee_policy
            return [self._exit(c, t, chain, now, None, None, policy_id) for t in held]

    def tick(self, *, now, raw_1m, chain, evidence, news_safe, fee_policy, admission_blocker=None):
        if now.utcoffset() is None:
            raise ValueError("Timezone-aware clock required")
        now = now.astimezone(IST)
        frame, candle_reason = session_frame(raw_1m, now, 1, holidays(self.settings))
        rows = (
            []
            if frame.empty
            else [
                {
                    "date": _start(r).replace(tzinfo=IST).isoformat(),
                    **{k: float(r[k]) for k in ("open", "high", "low", "close")},
                }
                for _, r in frame.iterrows()
            ]
        )
        spot = rows[-1]["close"] if rows else None
        reasons = []
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if paper_profit_trail.identity(self.settings.paper_candidate_profit_giveback_pct) != self.profit_identity:
                raise ValueError("Profit trail policy/source changed; create a new journal")
            paper_exit_policy.bind(c, self.settings.paper_exit_quote_policy, trades_table="trades")
            self._validate(c)
            prior = c.execute("SELECT body FROM state WHERE id=1").fetchone()
            state = json.loads(prior[0]) if prior else detector.initial_state(now.date().isoformat())
            if state["day"] != now.date().isoformat():
                state = detector.initial_state(now.date().isoformat())
            state, signals = self.signal_engine.advance(rows, state, now) if rows else (state, [])
            self.confirmed_signals = signals
            for held in c.execute(
                "SELECT * FROM trades WHERE state<>'CLOSED'"
            ).fetchall():
                reason = self._exit(
                    c, held, chain, now, spot, rows[-1] if rows else None, fee_policy
                )
                if reason:
                    reasons.append(reason)
            if not rows:
                return {
                    "state": "WAITING_FOR_1M",
                    "reasons": reasons + [candle_reason or "NO_CLOSED_1M"],
                }
            for signal in signals:
                if c.execute(
                    "SELECT 1 FROM decisions WHERE signal_id=?", (signal["signal_id"],)
                ).fetchone():
                    continue
                why = None
                age = (
                    now - datetime.fromisoformat(signal["signal_at"])
                ).total_seconds()
                if not market_status(now, self.settings)[0]:
                    why = "OUTSIDE_MARKET_SESSION"
                elif age < 0 or age > 90:
                    why = "SIGNAL_STALE_AT_RESTART"
                elif admission_blocker:
                    why = admission_blocker
                elif now.strftime("%H:%M") >= "15:00":
                    why = "ENTRY_CUTOFF"
                elif not news_safe:
                    why = "NEWS_UNAVAILABLE_OR_UNSAFE"
                elif not fee_policy:
                    why = "FEE_POLICY_UNVERIFIED"
                elif c.execute("SELECT 1 FROM trades WHERE state<>'CLOSED'").fetchone():
                    why = "POSITION_OR_EXIT_PENDING"
                # Fill only now, using the latest closed spot, never the historic signal price.
                entry_spot = spot
                sign = 1 if signal["side"] == "CALL" else -1
                risk = sign * (entry_spot - signal["stop_spot"])
                if why is None and not 10 <= risk <= 55:
                    why = "RISK_POINTS_OUTSIDE_10_55"
                strike = round(entry_spot / 50) * 50 + sign * 100
                kind = "CE" if sign == 1 else "PE"
                expiry = (chain or {}).get("expiry")
                q, identity_reason = self._quote(
                    chain, strike=strike, option_type=kind, expiry=expiry, now=now
                )
                why = why or (identity_reason if not self.dynamic_selection else None)
                if why:
                    self._decision(c, signal, now, "REJECTED", why)
                    reasons.append(why)
                    continue
                gross_loss = Decimal(0)
                for closed in c.execute(
                    "SELECT DISTINCT t.id FROM trades t JOIN fills f ON f.trade_id=t.id "
                    "WHERE t.state='CLOSED' AND f.side='SELL' AND substr(f.at,1,10)=?",
                    (now.date().isoformat(),),
                ):
                    gross_loss += max(Decimal(0), -self._pnl(c, closed[0]))
                cash = self._cash(c)
                strike, q, sized, selection = self._select(
                    chain=chain,evidence=evidence,now=now,entry_spot=entry_spot,
                    stop=signal['stop_spot'],sign=sign,kind=kind,expiry=expiry,
                    cash=cash,gross_loss=gross_loss,fee_policy=fee_policy)
                if sized.entry is None:
                    self._decision(c, signal, now, "REJECTED", sized.reason)
                    reasons.append(sized.reason)
                    continue
                entry = sized.entry
                body = {
                    **signal,
                    "day": now.date().isoformat(),
                    "opened_at": now.isoformat(),
                    "entry_spot": entry_spot,
                    "target_spot": round(entry_spot + sign * 3 * risk, 2),
                    "strike": strike,
                    "option_type": kind,
                    "expiry": expiry,
                    "lots": entry.lots,
                    "research_selection": selection,
                    "contract": q.tradingsymbol,
                    "instrument_id": q.provider_instrument_id,
                    "full_premium_loss": str(sized.full_premium_loss),
                    "stop_risk": str(sized.estimated_stop_risk),
                }
                if self.settings.paper_candidate_profit_giveback_pct is not None:
                    body["profit_exit"] = self.profit_profile
                c.execute(
                    "INSERT INTO trades VALUES (?,?,?,'OPEN',NULL)",
                    (signal["signal_id"], body["day"], encode(body)),
                )
                c.execute(
                    "INSERT INTO fills(trade_id,side,at,body) VALUES (?,?,?,?)",
                    (
                        signal["signal_id"],
                        "BUY",
                        now.isoformat(),
                        encode(asdict(entry)),
                    ),
                )
                self._decision(c, signal, now, "OPENED", "VERIFIED_PAPER_FILL")
                self._message(
                    c,
                    f"entry:{signal['signal_id']}",
                    f"ENTRY {now.isoformat()}\n{signal['side']} | {strike} {kind} | {expiry}\n"
                    f"NIFTY {entry_spot:.2f} | stop {body['stop_spot']:.2f} | "
                    + (f"net-profit giveback {self.settings.paper_candidate_profit_giveback_pct}% after 1R; no fixed target\n"
                       if self.settings.paper_candidate_profit_giveback_pct is not None else f"target {body['target_spot']:.2f}\n")
                    +
                    f"{entry.lots} lot(s), {entry.qty} units | simulated ask fill INR {entry.price}",
                )
            c.execute("INSERT OR REPLACE INTO state VALUES (1,?)", (encode(state),))
        return {
            "state": "SCANNING",
            "new_signals": len(signals),
            "reasons": sorted(set(reasons)),
        }

    def stats(self):
        with self.connect() as c:
            trades = c.execute("SELECT * FROM trades").fetchall()
            held = [r for r in trades if r["state"] != "CLOSED"]
            closed = [r for r in trades if r["state"] == "CLOSED"]
            realized = sum((self._pnl(c, r["id"]) for r in closed), Decimal(0))
            return {
                "label": "Candidate",
                "strategy_version": detector.VERSION,
                "capital_inr": str(CAPITAL),
                "cash_inr": str(self._cash(c)),
                "closed_net_pnl_inr": str(realized),
                "equity_inr": None if held else str(CAPITAL + realized),
                "entries": len(trades),
                "closed": len(closed),
                "held": len(held),
                "unresolved": sum(r["state"] == "EXIT_PENDING" for r in held),
                "rejections": [
                    dict(r)
                    for r in c.execute(
                        "SELECT reason,count(*) count FROM decisions WHERE outcome='REJECTED' GROUP BY reason"
                    )
                ],
                "notification_states": [
                    dict(r)
                    for r in c.execute(
                        "SELECT state,count(*) count FROM outbox GROUP BY state"
                    )
                ],
            }


class ResearchAccount(CandidateAccount):
    """Offline-only dynamic-strike and one-hop partial runner experiment."""
    def __init__(self,*args,dynamic_selection=False,hopping=False,levels_source=None,**kwargs):
        self.dynamic_selection=dynamic_selection
        self.hopping=hopping
        self.levels_source=levels_source
        super().__init__(*args,**kwargs)
        with self.connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS research_identity (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
            identity=encode(dict(dynamic_selection=dynamic_selection,hopping=hopping,
                version='dynamic-delta50-onehop-v1',source_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))
            previous=c.execute("SELECT body FROM research_identity WHERE id=1").fetchone()
            if previous and previous[0]!=identity: raise ValueError('Research policy changed')
            c.execute("INSERT OR IGNORE INTO research_identity VALUES(1,?)",(identity,))

    def _select(self,*,chain,evidence,now,entry_spot,stop,sign,kind,expiry,cash,gross_loss,fee_policy):
        steps=(-2,-1,0,1,2,3,4,5,6) if self.dynamic_selection else (2,)
        attempts=[]; passing=[]
        fallback=(None,None,paper_risk.SizingDecision(None,None,None,'NO_AFFORDABLE_DYNAMIC_CONTRACT'))
        for step in steps:
            strike=round(entry_spot/50)*50+sign*step*50
            q,why=self._quote(chain,strike=strike,option_type=kind,expiry=expiry,now=now)
            sized=paper_risk.SizingDecision(None,None,None,why) if why else paper_risk.size(
                quote=q,evidence=evidence.get(q.tradingsymbol),trusted_sources=frozenset({SOURCE}),
                now=now,settings=self.settings,underlying_entry=Decimal(str(entry_spot)),underlying_stop=Decimal(str(stop)),
                stop_budget=max(Decimal(0),min(Decimal(1000),cash*Decimal('.02'))).quantize(Decimal('.01'),rounding=ROUND_DOWN),
                gross_daily_loss=gross_loss,existing_reservations=Decimal(0),available_cash=cash,cost_policy_id=fee_policy)
            d=evidence.get(q.tradingsymbol) if q else None
            attempts.append(dict(step=step,strike=strike,delta=str(d.value) if d else None,
                reason=sized.reason,lots=sized.entry.lots if sized.entry else None))
            if step==2: fallback=(strike,q,sized)
            if sized.entry is not None:
                mid=(q.ask+q.bid)/2
                rank=(abs(abs(d.value)-Decimal('.50')),(q.ask-q.bid)/mid,sized.full_premium_loss,abs(step),step)
                passing.append((rank,strike,q,sized,step))
        if not passing:
            strike,q,sized=fallback
            return strike,q,sized,dict(attempts=attempts,chosen_step=None)
        _,strike,q,sized,step=min(passing,key=lambda x:x[0])
        return strike,q,sized,dict(attempts=attempts,chosen_step=step,
            preference='closest absolute delta to 0.50 among fully eligible contracts; spread, premium risk, distance tie-breaks')

    def _exit(self,c,trade_row,chain,now,spot,bar,fee_policy):
        if not self.hopping:
            return super()._exit(c,trade_row,chain,now,spot,bar,fee_policy)
        t=json.loads(trade_row['body']); remaining=t.get('remaining_lots',t['lots'])
        sign=1 if t['side']=='CALL' else -1
        stop=t.get('runner_stop',t['stop_spot']); target=t.get('runner_target',t['target_spot'])
        reason=trade_row['exit_reason']; partial=bool(t.get('pending_partial'))
        if reason and partial:
            eligible=bool(bar and datetime.fromisoformat(bar['date'])>=datetime.fromisoformat(t['opened_at']))
            stop_now=spot is not None and (sign*(spot-stop)<=0 or (eligible and (bar['low']<=stop if sign==1 else bar['high']>=stop)))
            if now.strftime('%H:%M')>='15:10' or t['day']<now.date().isoformat():
                reason='SQUARE_OFF'; partial=False; t.pop('pending_hop',None)
            elif stop_now:
                reason='STOP_TOUCH'; partial=False; t.pop('pending_hop',None)
        if not reason:
            if now.strftime('%H:%M')>='15:10' or t['day']<now.date().isoformat(): reason='SQUARE_OFF'
            elif spot is not None:
                eligible=bool(bar and datetime.fromisoformat(bar['date'])>=datetime.fromisoformat(t['opened_at']))
                stop_touch=sign*(spot-stop)<=0 or (eligible and (bar['low']<=stop if sign==1 else bar['high']>=stop))
                target_touch=sign*(spot-target)>=0 or (eligible and (bar['high']>=target if sign==1 else bar['low']<=target))
                if stop_touch: reason='RUNNER_STOP' if t.get('has_hopped') else 'STOP_TOUCH'
                elif target_touch:
                    partial=remaining>=2 and not t.get('has_hopped')
                    reason='LIQUIDITY_HOP_PARTIAL' if partial else 'RUNNER_TARGET' if t.get('has_hopped') else 'TARGET_TOUCH'
                    if partial:
                        levels=sorted(set(float(x) for x in self.levels_source(now)))
                        ahead=[x for x in levels if sign*(x-target)>5]
                        next_target=min(ahead,key=lambda x:sign*(x-target)) if ahead else target+sign*1.5*t['risk_points']
                        t['pending_hop']=dict(at=now.isoformat(),conquered=target,next_target=round(next_target,2),
                            ratcheted_stop=round(target-sign*3,2),known_levels=levels,
                            source='causal_known_level' if ahead else '1.5R_extension_fallback')
                elif t.get('has_hopped') and sign*(spot-t['entry_spot'])/t['risk_points']>=1.5:
                    five=self.five_source(now)
                    if len(five)>=2:
                        last,prior=five[-1],five[-2]
                        if (last['close']<prior['low'] if sign==1 else last['close']>prior['high']): reason='RUNNER_STRUCTURE'
        if not reason: return None
        q,why=self._quote(chain,strike=t['strike'],option_type=t['option_type'],expiry=t['expiry'],now=now)
        entry=json.loads(c.execute("SELECT body FROM fills WHERE trade_id=? AND side='BUY'",(t['signal_id'],)).fetchone()[0])
        if q is not None and (q.tradingsymbol!=t['contract'] or q.provider_instrument_id!=t['instrument_id'] or q.lot_size*t['lots']!=entry['qty']):
            q,why=None,'CONTRACT_IDENTITY_MISMATCH'
        q,quote_why=px.exit_executable(q,now,self.settings); why=why or quote_why
        sell_lots=remaining//2 if partial else remaining
        if q is None or not fee_policy:
            why=why or 'FEE_POLICY_UNVERIFIED'
            t['pending_partial']=partial
            c.execute("UPDATE trades SET state='EXIT_PENDING',exit_reason=?,body=? WHERE id=?",(reason,encode(t),t['signal_id']))
            return why
        try:
            sold=px.fill(q,'SELL',sell_lots,now=now,settings=self.settings,ticks=paper_risk.SLIPPAGE_TICKS,cost_policy_id=fee_policy)
            # A partial must not add negative liquidation proceeds and an extra
            # order fee to the entry's full-premium worst-case reservation.
            # Preview fills are pure: only the selected SELL is posted below.
            if partial and sold.value-sold.charges_inr<=0:
                partial=False; sell_lots=remaining; reason='TARGET_NO_ECONOMIC_PARTIAL'
                t.pop('pending_hop',None)
                sold=px.fill(q,'SELL',sell_lots,now=now,settings=self.settings,ticks=paper_risk.SLIPPAGE_TICKS,cost_policy_id=fee_policy)
        except ValueError as exc:
            t['pending_partial']=partial
            c.execute("UPDATE trades SET state='EXIT_PENDING',exit_reason=?,body=? WHERE id=?",(reason,encode(t),t['signal_id']))
            return str(exc)
        c.execute("INSERT INTO fills(trade_id,side,at,body) VALUES (?,?,?,?)",(t['signal_id'],'SELL',now.isoformat(),encode(asdict(sold))))
        remaining-=sell_lots; t['remaining_lots']=remaining
        t.setdefault('research_exit_events',[]).append(dict(at=now.isoformat(),reason=reason,lots=sell_lots,
            trigger_spot=spot,stop_before=stop,target_before=target,fill_price=str(sold.price)))
        t.pop('pending_partial',None)
        if partial:
            hop=t.pop('pending_hop'); hop['filled_at']=now.isoformat(); hop['booked_lots']=sell_lots
            t['hop']=hop; t['has_hopped']=True
            t['runner_target']=hop['next_target']; t['runner_stop']=max(stop,hop['ratcheted_stop']) if sign==1 else min(stop,hop['ratcheted_stop'])
        state='CLOSED' if remaining==0 else 'OPEN'
        c.execute("UPDATE trades SET body=?,state=?,exit_reason=? WHERE id=?",(encode(t),state,reason if remaining==0 else None,t['signal_id']))
        self._message(c,f"research-exit:{t['signal_id']}:{len(t['research_exit_events'])}",f"OFFLINE {reason}: {sell_lots} lots")
        return None


from app.trading import hybrid_research_policy as hybrid

class HybridAccount(ResearchAccount):
    def __init__(self,*args,signal_engine,legacy_management=False,**kwargs):
        self.signal_engine=signal_engine
        self.legacy_management=legacy_management
        self.confirmed_signals=[]
        super().__init__(*args,**kwargs)
        with self.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS hybrid_identity(id INTEGER PRIMARY KEY,body TEXT NOT NULL)')
            value=encode(dict(version=hybrid.VERSION,policy_hash=hybrid.identity(),
                broader_entries=signal_engine.broader,legacy_management=legacy_management))
            previous=c.execute('SELECT body FROM hybrid_identity WHERE id=1').fetchone()
            if previous and previous[0]!=value:raise ValueError('Hybrid policy changed: use a new journal')
            c.execute('INSERT OR IGNORE INTO hybrid_identity VALUES(1,?)',(value,))

    def _exit(self,c,trade_row,chain,now,spot,bar,fee_policy):
        if not self.legacy_management or spot is None:
            return super()._exit(c,trade_row,chain,now,spot,bar,fee_policy)
        t=json.loads(trade_row['body'])
        # A committed full-exit intent cannot be cancelled by a later HOLD.
        if trade_row['exit_reason'] and not t.get('pending_partial'):
            return super()._exit(c,trade_row,chain,now,spot,bar,fee_policy)
        plan=hybrid.legacy_exit(t,spot=spot,bar=bar,five=self.five_source(now),
            levels=self.levels_source(now),signals=self.confirmed_signals,now=now)
        if plan['action']=='HOLD' and not t.get('pending_partial'):
            updates=plan['updates']
            before=t.get('runner_stop',t['stop_spot'])
            if updates['runner_stop']!=before or updates['research_milestones']!=t.get('research_milestones',[]):
                t.setdefault('management_updates',[]).append(dict(at=now.isoformat(),stop_before=before,
                    stop_after=updates['runner_stop'],milestones=updates['research_milestones']))
            t.update(updates)
            c.execute('UPDATE trades SET body=? WHERE id=?',(encode(t),t['signal_id']))
            return None
        proxy=dict(trade_row)
        if plan.get('is_fully_closed'):
            t.pop('pending_partial',None);t.pop('pending_hop',None)
            proxy['body']=encode(t);proxy['exit_reason']=plan['action']
        return super()._exit(c,proxy,chain,now,spot,bar,fee_policy)
