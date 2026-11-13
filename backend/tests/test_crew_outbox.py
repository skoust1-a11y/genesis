"""S1-P4 durable Telegram outbox. Temp journals, fake transports, sockets blocked. No real Telegram."""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import socket
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.config import get_settings
from app.db import crew_journal as cj
from app.db import crew_outbox as ob
from app.db import crew_session as cs
from app.db import trade_store
from app.telegram_transport import (DELIVERED, NOT_DELIVERED, UNKNOWN, DeliveryOutcome, TelegramTransport,
                                    classify_response)
from app.trading import admission as adm
from app.trading.option_quote import IST, ContractQuote

DAY = "2026-11-06"
CANDLE = "2026-11-06T10:15:00+05:30"
NOW = datetime(2026, 11, 6, 10, 15, 5, tzinfo=IST)
# Generated dummy has parser-required syntax; all transport is mocked and sockets blocked.
TOKEN = "000000000:" + "SYNTHETIC_TEST_TOKEN_DO_NOT_USE_00000"
PROD_DB = trade_store.DEFAULT_DB_PATH.resolve()
OCT30 = trade_store.ROOT_DIR / "trades" / ".active_trades_2026-10-30.json"
OK = DeliveryOutcome(DELIVERED, 200, True, 42)
CHAT = "-100"
NETWORK_CALLS: list = []


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def blocked(*args, **_):
        NETWORK_CALLS.append(args)
        raise AssertionError("network call in outbox tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


LEASES: dict[Path, cs.Lease] = {}


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "journal.db"
    cj.migrate(p)
    LEASES[p] = cs.acquire(p, now=NOW, ttl_seconds=3600)
    return p


def L(path):
    """The journal's current session lease."""
    return LEASES[path]


def handover(path, at=NOW):
    """The current owner stops and a new session takes the journal; returns the new lease."""
    cs.release(path, LEASES[path], reason="test handover", now=at)
    LEASES[path] = cs.acquire(path, now=at, ttl_seconds=3600)
    return LEASES[path]


def trade(path, setup="OR-30"):
    """An ACTIONABLE trade built through the P2 API. P3 itself never produces one."""
    a = cj.record_admission(path, lease=L(path), now=NOW, admission_class="ACTIONABLE", reasons=[], market_day=DAY,
                            instrument="NIFTY", strategy_id="PDC Zone", strategy_version="v0",
                            signal_candle=CANDLE, direction="CALL", setup_id=setup)
    return cj.open_trade(path, lease=L(path), now=NOW, admission_id=a["admission"]["admission_id"], model_lots=3,
                         entry_spot=23100, stop_spot=23080, target_spot=23160)["trade"]["trade_id"]


def rec(path, tid, body="BUY NIFTY 26100 CE", now=NOW, purpose="entry", chat=CHAT):
    return ob.enqueue_publication(path, lease=L(path), kind="RECOMMENDATION", purpose=purpose, body=body, trade_id=tid,
                                  chat_id=chat, now=now)["publication"]


def ops(path, purpose="halt", body="crew halted", chat=CHAT):
    return ob.enqueue_publication(path, lease=L(path), kind="OPS", purpose=purpose, body=body, chat_id=chat, now=NOW)["publication"]


class Fake:
    def __init__(self, *outcomes):
        self.outcomes, self.sent = list(outcomes), []

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


def http(handler, calls=None):
    def wrapped(request):
        if calls is not None:
            calls.append(request)
        return handler(request)

    return TelegramTransport(TOKEN, timeout=1.5, client=httpx.Client(transport=httpx.MockTransport(wrapped)))


def deliver(path, pid, transport, at=NOW):
    return ob.deliver_publication(path, pid, transport=transport, lease=L(path), clock=lambda: at)


def events(path, pid):
    return ob.inspect_publication(path, pid)["events"]


def reply(status, body):
    return lambda _req: httpx.Response(status, content=body if isinstance(body, bytes) else json.dumps(body).encode())


def raise_(exc):
    def handler(request):
        raise exc(f"boom for {request.url}", request=request)
    return handler


# --- enqueue and claim -----------------------------------------------------------------------------

def test_01_duplicate_enqueue_returns_existing(path):
    tid = trade(path)
    first = ob.enqueue_publication(path, lease=L(path), kind="RECOMMENDATION", purpose="entry", body="b", trade_id=tid,
                                   chat_id=CHAT, now=NOW)
    again = ob.enqueue_publication(path, lease=L(path), kind="RECOMMENDATION", purpose=" entry ", body="b", trade_id=tid,
                                   chat_id=CHAT, now=NOW + timedelta(seconds=5))
    assert first["replayed"] is False and again["replayed"] is True
    assert again["publication"] == first["publication"]
    assert first["publication"]["publication_id"] == ob.publication_id_for(kind="RECOMMENDATION", purpose="entry",
                                                                           trade_id=tid)
    with pytest.raises(cj.InvalidTransition, match="different body"):
        rec(path, tid, body="SELL")
    assert len(events(path, first["publication"]["publication_id"])) == 1


def test_02_concurrent_enqueue_creates_one_row(path):
    tid, barrier, results = trade(path), threading.Barrier(8), []

    def worker():
        barrier.wait()
        results.append(ob.enqueue_publication(path, lease=L(path), kind="RECOMMENDATION", purpose="entry", body="b",
                                              trade_id=tid, chat_id=CHAT, now=NOW))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(r["replayed"] for r in results) == [False] + [True] * 7
    conn = cj.connect(path)
    assert conn.execute("SELECT count(*) FROM crew_outbox").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM crew_outbox_events").fetchone()[0] == 1
    conn.close()


def test_03_concurrent_claim_has_one_winner(path):
    pid, barrier, won, lost = rec(path, trade(path))["publication_id"], threading.Barrier(8), [], []

    def worker(i):
        barrier.wait()
        try:
            won.append(ob.claim_publication(path, pid, lease=L(path), now=NOW))
        except cj.InvalidTransition as e:
            lost.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(won) == 1 and len(lost) == 7 and won[0]["state"] == "SENDING" and won[0]["attempts"] == 1
    assert [e["to_state"] for e in events(path, pid)] == ["PENDING", "SENDING"]


# --- HTTP classification through the production transport class (mocked HTTP layer) -----------------

CASES = {
    "ack": (reply(200, {"ok": True, "result": {"message_id": 42}}), DELIVERED, "ACKED"),
    "missing_message_id": (reply(200, {"ok": True, "result": {}}), UNKNOWN, "UNKNOWN"),
    "bool_message_id": (reply(200, {"ok": True, "result": {"message_id": True}}), UNKNOWN, "UNKNOWN"),
    "invalid_success_body": (reply(200, b"<html>ok</html>"), UNKNOWN, "UNKNOWN"),
    "ok_false_on_200": (reply(200, {"ok": False, "result": {"message_id": 5}}), UNKNOWN, "UNKNOWN"),
    "read_timeout": (raise_(httpx.ReadTimeout), UNKNOWN, "UNKNOWN"),
    "connect_timeout": (raise_(httpx.ConnectTimeout), UNKNOWN, "UNKNOWN"),
    "connection_reset": (raise_(httpx.ReadError), UNKNOWN, "UNKNOWN"),
    "remote_protocol": (raise_(httpx.RemoteProtocolError), UNKNOWN, "UNKNOWN"),
    "http_429": (reply(429, {"ok": False, "error_code": 429, "description": "Too Many Requests",
                             "parameters": {"retry_after": 7}}), NOT_DELIVERED, "RETRY_WAIT"),
    "http_429_malformed": (reply(429, b"rate limited"), UNKNOWN, "UNKNOWN"),
    "http_400": (reply(400, {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"}),
                 NOT_DELIVERED, "FAILED_PERMANENT"),
    "http_400_malformed": (reply(400, b""), UNKNOWN, "UNKNOWN"),
    "http_500": (reply(500, {"ok": False, "error_code": 500, "description": "Internal"}), UNKNOWN, "UNKNOWN"),
    "http_502": (reply(502, b"bad gateway"), UNKNOWN, "UNKNOWN"),
}


@pytest.mark.parametrize("name", list(CASES))
def test_04_to_12_delivery_classification(path, name):
    handler, status, state = CASES[name]
    calls = []
    pid = rec(path, trade(path))["publication_id"]
    final = deliver(path, pid, http(handler, calls))
    assert final["state"] == state and len(calls) == 1
    sent = json.loads(calls[0].content)
    assert sent == {"chat_id": "-100", "text": "BUY NIFTY 26100 CE"}  # no parse_mode, no hidden fallback
    last = events(path, pid)[-1]
    assert last["to_state"] == state and last["session_id"] == L(path).session_id
    if state == "ACKED":
        assert final["telegram_message_id"] == 42 and last["message_id"] == 42 and last["http_status"] == 200
    else:
        assert final["telegram_message_id"] is None
    if state == "RETRY_WAIT":
        assert final["not_before"] == (NOW + timedelta(seconds=7)).isoformat()
    if name in ("read_timeout", "connect_timeout", "connection_reset", "remote_protocol"):
        assert last["exc_class"] == handler.__closure__[0].cell_contents.__name__
    assert http(handler).send("-100", "x").status == status


def test_10_http_429_retry_is_bounded_by_the_schema(path):
    pid = rec(path, trade(path))["publication_id"]
    limited = DeliveryOutcome(NOT_DELIVERED, 429, False, retry_after=3)
    fake = Fake(limited, limited, limited)
    assert deliver(path, pid, fake)["state"] == "RETRY_WAIT"
    for n, expected in ((1, "RETRY_WAIT"), (2, "FAILED_PERMANENT")):
        due = NOW + timedelta(seconds=3 * n)
        with pytest.raises(cj.InvalidTransition, match="not due"):
            ob.claim_publication(path, pid, lease=L(path), now=due - timedelta(seconds=1))
        assert pid in [p["publication_id"] for p in ob.list_pending_publications(path, now=due)]
        assert deliver(path, pid, fake, at=due)["state"] == expected
    assert len(fake.sent) == 3
    assert ob.inspect_publication(path, pid)["publication"]["attempts"] == 3
    with pytest.raises(cj.InvalidTransition, match="FAILED_PERMANENT"):
        deliver(path, pid, Fake(OK), at=NOW + timedelta(seconds=30))


def test_11_http_400_is_never_resent_as_plain_text(path):
    calls = []
    pid = rec(path, trade(path))["publication_id"]
    deliver(path, pid, http(CASES["http_400"][0], calls))
    assert len(calls) == 1
    with pytest.raises(cj.InvalidTransition):
        deliver(path, pid, http(CASES["ack"][0], calls))
    assert len(calls) == 1


def test_pre_send_validation_is_not_delivered_without_a_request():
    calls = []
    t = http(CASES["ack"][0], calls)
    assert t.send("-100", "").status == NOT_DELIVERED and t.send("", "x").status == NOT_DELIVERED
    assert t.send("-100", "x" * 4097).exc_class == "PreSendValidation" and calls == []


def test_raising_transport_is_unknown(path):
    pid = rec(path, trade(path))["publication_id"]
    final = deliver(path, pid, Fake(ConnectionResetError("reset by peer")))
    assert final["state"] == "UNKNOWN" and events(path, pid)[-1]["exc_class"] == "ConnectionResetError"


# --- crash and recovery ----------------------------------------------------------------------------

def test_13_crash_before_http(path, monkeypatch):
    pid = rec(path, trade(path))["publication_id"]

    def die(*_, **__):
        raise KeyboardInterrupt("killed during claim")

    monkeypatch.setattr(ob, "_transition", die)
    fake = Fake(OK)
    with pytest.raises(KeyboardInterrupt):
        deliver(path, pid, fake)
    monkeypatch.undo()
    assert ob.inspect_publication(path, pid)["publication"]["state"] == "PENDING" and fake.sent == []

    # Killed after the claim committed but before transport.send: still ambiguous to the next process.
    ob.claim_publication(path, pid, lease=L(path), now=NOW)
    nxt = handover(path)
    assert [r["publication_id"] for r in ob.recover_inflight_publications(path, lease=nxt, now=NOW)] == [pid]
    assert ob.inspect_publication(path, pid)["publication"]["state"] == "UNKNOWN" and fake.sent == []


def test_14_crash_after_http_before_ack(path, monkeypatch):
    pid = rec(path, trade(path))["publication_id"]
    fake = Fake(OK, OK)

    def die(*_, **__):
        raise KeyboardInterrupt("killed after HTTP")

    monkeypatch.setattr(ob, "record_outcome", die)
    with pytest.raises(KeyboardInterrupt):
        deliver(path, pid, fake)
    monkeypatch.undo()
    assert len(fake.sent) == 1
    assert ob.inspect_publication(path, pid)["publication"]["state"] == "SENDING"
    later = NOW + timedelta(seconds=30)
    ob.recover_inflight_publications(path, lease=handover(path, later), now=later)
    with pytest.raises(cj.InvalidTransition, match="UNKNOWN"):
        deliver(path, pid, fake, at=later)
    assert len(fake.sent) == 1


def test_15_recovery_from_sending_preserves_evidence(path):
    tid = trade(path)
    old = rec(path, tid)["publication_id"]
    mine = ops(path)["publication_id"]
    s1 = L(path)
    ob.claim_publication(path, old, lease=s1, now=NOW)
    s2 = handover(path)
    ob.claim_publication(path, mine, lease=s2, now=NOW)
    recovered = ob.recover_inflight_publications(path, lease=s2, now=NOW + timedelta(seconds=60))
    assert [r["publication_id"] for r in recovered] == [old] and recovered[0]["state"] == "UNKNOWN"
    assert ob.inspect_publication(path, mine)["publication"]["state"] == "SENDING"
    evs = events(path, old)
    assert [(e["seq"], e["from_state"], e["to_state"]) for e in evs] == [
        (1, None, "PENDING"), (2, "PENDING", "SENDING"), (3, "SENDING", "UNKNOWN")]
    assert evs[1]["session_id"] == s1.session_id and evs[2]["session_id"] == s2.session_id
    assert f"RECOVERED_INFLIGHT claimed by {s1.session_id}" in evs[2]["note"]
    row = ob.inspect_publication(path, old)["publication"]
    assert row["claimed_by_session"] == s1.session_id and row["claimed_at"] == NOW.isoformat() and row["attempts"] == 1
    assert ob.recover_inflight_publications(path, lease=s2, now=NOW) == []


def test_16_unknown_never_resends_and_blocks_recommendations(path):
    first, second = trade(path, "A"), trade(path, "B")
    waiting = rec(path, second, purpose="entry-b")["publication_id"]
    pid = rec(path, first)["publication_id"]
    deliver(path, pid, Fake(DeliveryOutcome(UNKNOWN, exc_class="ReadTimeout")))
    for _ in range(3):
        ob.recover_inflight_publications(path, lease=L(path), now=NOW)
        assert pid not in [p["publication_id"] for p in ob.list_pending_publications(path, now=NOW)]
        with pytest.raises(cj.InvalidTransition):
            ob.claim_publication(path, pid, lease=L(path), now=NOW)
    with pytest.raises(cj.InvalidTransition, match="BLOCKED"):
        ob.claim_publication(path, waiting, lease=L(path), now=NOW)
    third = trade(path, "C")
    blocked = rec(path, third, purpose="entry-c")
    assert blocked["state"] == "SUPPRESSED" and "BLOCKED" in events(path, blocked["publication_id"])[0]["note"]
    assert deliver(path, ops(path)["publication_id"], Fake(OK))["state"] == "ACKED"
    assert ob.inspect_publication(path, pid)["publication"]["state"] == "UNKNOWN"
    ob.resolve_unknown(path, pid, operator="skoust", delivered=False, evidence="no message in -100", now=NOW)
    assert ob.claim_publication(path, waiting, lease=L(path), now=NOW)["state"] == "SENDING"
    assert ob.inspect_publication(path, blocked["publication_id"])["publication"]["state"] == "SUPPRESSED"


def test_17_operator_resolution(path):
    pid = rec(path, trade(path))["publication_id"]
    deliver(path, pid, Fake(DeliveryOutcome(UNKNOWN, 502)))
    for bad in ({"operator": "", "evidence": "x", "delivered": False},
                {"operator": "skoust", "evidence": "  ", "delivered": False},
                {"operator": "skoust", "evidence": "seen", "delivered": True},
                {"operator": "skoust", "evidence": "absent", "delivered": False, "message_id": 9},
                {"operator": "skoust", "evidence": "absent", "delivered": "no"}):
        with pytest.raises(cj.InvalidTransition):
            ob.resolve_unknown(path, pid, now=NOW, **bad)
    at = NOW + timedelta(minutes=5)
    done = ob.resolve_unknown(path, pid, operator="skoust", delivered=True, message_id=77, now=at,
                              evidence="channel -100 shows message 77 at 10:15:06 with this body")
    assert done["state"] == "RESOLVED_DELIVERED" and done["telegram_message_id"] == 77
    last = events(path, pid)[-1]
    assert (last["operator"], last["method"], last["at"], last["message_id"]) == ("skoust", "channel_inspection",
                                                                                at.isoformat(), 77)
    assert "message 77" in last["note"]
    with pytest.raises(cj.InvalidTransition):
        ob.resolve_unknown(path, pid, operator="skoust", delivered=False, evidence="x", now=at)
    monitor = ob.enqueue_publication(path, lease=L(path), kind="MONITORING", purpose="target-hit", body="target hit",
                                     trade_id=done["trade_id"], parent_publication_id=pid, chat_id=CHAT, now=at)
    assert monitor["publication"]["state"] == "PENDING"


def test_17b_resolved_not_delivered_is_terminal(path):
    pid = rec(path, trade(path))["publication_id"]
    deliver(path, pid, Fake(DeliveryOutcome(UNKNOWN, exc_class="ReadTimeout")))
    done = ob.resolve_unknown(path, pid, operator="skoust", delivered=False, now=NOW,
                              evidence="channel -100 has no message after 10:15:00")
    assert done["state"] == "RESOLVED_NOT_DELIVERED" and done["telegram_message_id"] is None
    with pytest.raises(cj.InvalidTransition):
        ob.claim_publication(path, pid, lease=L(path), now=NOW)


# --- publication rules -----------------------------------------------------------------------------

def test_18_stale_recommendation_is_suppressed(path):
    tid = trade(path)
    pid = rec(path, tid)["publication_id"]
    fake = Fake(OK)
    late = NOW + timedelta(seconds=ob.RECOMMENDATION_MAX_AGE_SECONDS + 1)
    assert deliver(path, pid, fake, at=late)["state"] == "SUPPRESSED" and fake.sent == []
    assert "STALE" in events(path, pid)[-1]["note"]
    trade_row = cj.load_open_trades(path)[0]
    cj.exit_trade(path, lease=L(path), now=NOW, trade_id=tid, expected_version=trade_row["version"],
                  reason="target", signal_candle="2026-11-06T10:20:00+05:30", index_levels={"spot": 23160})
    assert rec(path, tid, purpose="after-exit")["state"] == "SUPPRESSED"
    yesterday, tomorrow = trade(path, "yesterday"), NOW + timedelta(days=1)
    handover(path, tomorrow)
    assert rec(path, yesterday, purpose="next-day", now=tomorrow)["state"] == "SUPPRESSED"
    assert "another market day" in events(path, ob.publication_id_for(
        kind="RECOMMENDATION", purpose="next-day", trade_id=yesterday))[0]["note"]


def test_18b_expiry_runs_from_admission_not_enqueue(path):
    decided = datetime.fromisoformat(CANDLE)
    limit = timedelta(seconds=ob.RECOMMENDATION_MAX_AGE_SECONDS)
    late_enqueue = rec(path, trade(path, "A"), now=decided + limit + timedelta(seconds=1))
    assert late_enqueue["state"] == "SUPPRESSED" and "since admission" in events(path, late_enqueue["publication_id"])[0]["note"]
    fresh = rec(path, trade(path, "B"), now=decided + limit - timedelta(seconds=20))["publication_id"]
    fake = Fake(OK)
    assert deliver(path, fresh, fake, at=decided + limit + timedelta(seconds=1))["state"] == "SUPPRESSED"
    assert fake.sent == []
    edge = rec(path, trade(path, "C"), now=decided)["publication_id"]
    assert ob.claim_publication(path, edge, lease=L(path), now=decided + limit)["state"] == "SENDING"


def test_18c_admission_time_without_timezone_is_stale(path):
    a = cj.record_admission(path, lease=L(path), now=NOW, admission_class="ACTIONABLE", reasons=[], market_day=DAY,
                            instrument="NIFTY", strategy_id="PDC Zone", strategy_version="v0",
                            signal_candle="2026-11-06T10:15:00", direction="CALL", setup_id="naive")
    tid = cj.open_trade(path, lease=L(path), now=NOW, admission_id=a["admission"]["admission_id"], model_lots=3,
                        entry_spot=23100, stop_spot=23080, target_spot=23160)["trade"]["trade_id"]
    assert "no timezone" in events(path, rec(path, tid)["publication_id"])[0]["note"]


QUOTE = ContractQuote("NIFTY", "2026-11-10", 26100.0, "CE", "provider-1", "NIFTY25N1026100CE", 75,
                      Decimal("120.5"), None, None, None, NOW - timedelta(seconds=4), "chain", 0.42)
CANDIDATE = adm.SignalCandidate(DAY, "NIFTY", "PDC Zone", "v0", CANDLE, "CALL", "OR-30")
SETTINGS = get_settings().model_copy(update={"max_spread_pct": 5})


def test_19_research_only_cannot_publish_and_p3_never_yields_actionable(path):
    executable = replace(QUOTE, bid=Decimal("120"), ask=Decimal("121"), quote_time=NOW - timedelta(seconds=2))
    classes = {adm.evaluate(replace(CANDIDATE, setup_id=f"s{i}"), q, now=NOW, settings=SETTINGS).admission_class
               for i, q in enumerate((QUOTE, executable, None, replace(QUOTE, ltp=None)))}
    assert "ACTIONABLE" not in classes
    persisted = adm.persist(path, adm.evaluate(CANDIDATE, executable, now=NOW, settings=SETTINGS), lease=L(path),
                            now=NOW)
    tid = adm.open_research_trade(path, persisted, lease=L(path), now=NOW, model_lots=3, entry_spot=23100,
                                  stop_spot=23080, target_spot=23160)["trade"]["trade_id"]
    for kind in ("RECOMMENDATION", "MONITORING"):
        with pytest.raises(cj.InvalidTransition, match="ACTIONABLE"):
            ob.enqueue_publication(path, lease=L(path), kind=kind, purpose="entry", body="b", trade_id=tid, chat_id=CHAT,
                                   parent_publication_id="0" * 64 if kind == "MONITORING" else None, now=NOW)
    assert ob.list_pending_publications(path, now=NOW) == []


def test_20_rejected_admission_cannot_publish(path):
    rejected = adm.evaluate(CANDIDATE, None, now=NOW, settings=SETTINGS)
    stored = adm.persist(path, rejected, lease=L(path), now=NOW)["admission"]
    assert stored["class"] == "REJECTED"
    tid = cj.trade_id_for(DAY, stored["signal_id"])
    with pytest.raises(cj.InvalidTransition, match="ACTIONABLE"):
        rec(path, tid)
    with pytest.raises(cj.InvalidTransition):
        ob.enqueue_publication(path, lease=L(path), kind="OPS", purpose="x", body="b", trade_id=tid, chat_id=CHAT, now=NOW)


def test_21_monitoring_needs_a_delivered_parent(path):
    tid = trade(path)
    parent = rec(path, tid)["publication_id"]
    kw = {"kind": "MONITORING", "purpose": "stop-moved", "body": "stop moved", "trade_id": tid, "chat_id": CHAT,
          "now": NOW}
    with pytest.raises(cj.InvalidTransition, match="delivered RECOMMENDATION"):
        ob.enqueue_publication(path, lease=L(path), parent_publication_id=parent, **kw)
    with pytest.raises(cj.InvalidTransition, match="delivered RECOMMENDATION"):
        ob.enqueue_publication(path, lease=L(path), parent_publication_id=None, **kw)
    with pytest.raises(cj.InvalidTransition, match="delivered RECOMMENDATION"):
        ob.enqueue_publication(path, lease=L(path), parent_publication_id=ops(path)["publication_id"], **kw)
    deliver(path, parent, Fake(OK))
    assert ob.enqueue_publication(path, lease=L(path), parent_publication_id=parent, **kw)["publication"]["state"] == "PENDING"


def test_22_duplicate_telegram_message_id_is_not_acked(path):
    a = rec(path, trade(path, "A"), purpose="a")["publication_id"]
    b = ops(path)["publication_id"]
    assert deliver(path, a, Fake(OK))["state"] == "ACKED"
    second = deliver(path, b, Fake(OK))
    assert second["state"] == "UNKNOWN" and second["telegram_message_id"] is None
    last = events(path, b)[-1]
    assert last["message_id"] == 42 and "already belongs to" in last["note"]
    with pytest.raises(cj.InvalidTransition, match="already belongs"):
        ob.resolve_unknown(path, b, operator="skoust", delivered=True, message_id=42, evidence="seen", now=NOW)


def test_22b_message_identity_is_per_chat_and_the_destination_is_bound(path):
    tid = trade(path)
    parent = rec(path, tid)["publication_id"]
    assert deliver(path, parent, Fake(OK))["state"] == "ACKED"
    fake = Fake(OK)
    assert deliver(path, ops(path, chat="-200")["publication_id"], fake)["state"] == "ACKED"  # 42 in another chat
    assert fake.sent == [("-200", "crew halted")]
    with pytest.raises(cj.InvalidTransition, match="different chat"):
        rec(path, tid, chat="-200")
    with pytest.raises(cj.InvalidTransition, match="chat_id"):
        ops(path, purpose="blank", chat=" -100")
    with pytest.raises(cj.InvalidTransition, match="parent's chat"):
        ob.enqueue_publication(path, lease=L(path), kind="MONITORING", purpose="m", body="m", trade_id=tid,
                               parent_publication_id=parent, chat_id="-200", now=NOW)
    conn = cj.connect(path)
    insert = ("INSERT INTO crew_outbox (publication_id, trade_id, parent_publication_id, kind, purpose, body,"
              " body_sha256, state, created_at, chat_id) VALUES (?, ?, ?, ?, 'm', 'm', ?, 'PENDING', ?, ?)")
    with pytest.raises(sqlite3.IntegrityError, match="parent's chat"):
        conn.execute(insert, ("a" * 64, tid, parent, "MONITORING", "0" * 64, NOW.isoformat(), "-200"))
    with pytest.raises(sqlite3.IntegrityError, match="destination chat_id"):
        conn.execute(insert, ("b" * 64, None, None, "OPS", "0" * 64, NOW.isoformat(), None))
    pending = ops(path, purpose="p")["publication_id"]
    conn.execute("INSERT INTO crew_outbox_events (publication_id, seq, from_state, to_state, at, note)"
                 " VALUES (?, 2, 'PENDING', 'SUPPRESSED', ?, 'x')", (pending, NOW.isoformat()))
    with pytest.raises(sqlite3.IntegrityError, match="identity columns are immutable"):
        conn.execute("UPDATE crew_outbox SET state='SUPPRESSED', version=2, chat_id='-300' WHERE publication_id=?",
                     (pending,))
    conn.close()


def test_22c_v1_journal_upgrades_without_reinterpreting_delivery_evidence(tmp_path):
    p, at = tmp_path / "v1.db", NOW.isoformat()
    conn = cj.connect(p, create=True)
    conn.executescript(f"BEGIN IMMEDIATE;\n{cj.SCHEMA}\nCOMMIT;")  # the v1 DDL, before the chat_id column
    conn.execute("UPDATE crew_meta SET value='1' WHERE key='schema_version'")
    conn.execute("INSERT INTO crew_sessions (session_id, market_day, started_at) VALUES ('S1', ?, ?)", (DAY, at))
    acked, pending = (ob.publication_id_for(kind="OPS", purpose=w) for w in ("old-ack", "old-pending"))
    for pid in (acked, pending):
        conn.execute("INSERT INTO crew_outbox (publication_id, kind, purpose, body, body_sha256, state, created_at)"
                     " VALUES (?, 'OPS', 'old', 'b', ?, 'PENDING', ?)", (pid, hashlib.sha256(b"b").hexdigest(), at))
        conn.execute("INSERT INTO crew_outbox_events (publication_id, seq, to_state, at) VALUES (?, 1, 'PENDING', ?)",
                     (pid, at))
    conn.execute("INSERT INTO crew_outbox_events (publication_id, seq, from_state, to_state, at, session_id)"
                 " VALUES (?, 2, 'PENDING', 'SENDING', ?, 'S1')", (acked, at))
    conn.execute("UPDATE crew_outbox SET state='SENDING', version=2, attempts=1, claimed_at=?, claimed_by_session='S1'"
                 " WHERE publication_id=?", (at, acked))
    conn.execute("INSERT INTO crew_outbox_events (publication_id, seq, from_state, to_state, at, session_id,"
                 " http_status, telegram_ok, message_id) VALUES (?, 3, 'SENDING', 'ACKED', ?, 'S1', 200, 1, 42)",
                 (acked, at))
    conn.execute("UPDATE crew_outbox SET state='ACKED', version=3, acked_at=?, telegram_message_id=42"
                 " WHERE publication_id=?", (at, acked))
    conn.close()
    before = ob.inspect_publication(p, acked)

    assert cj.migrate(p) == cj.SCHEMA_VERSION and cj.migrate(p) == cj.SCHEMA_VERSION
    after = ob.inspect_publication(p, acked)
    assert after == {**before, "publication": {**before["publication"], "chat_id": None}}
    LEASES[p] = cs.acquire(p, now=NOW, ttl_seconds=3600)
    with pytest.raises(cj.InvalidTransition, match="no recorded chat"):
        ob.claim_publication(p, pending, lease=L(p), now=NOW)
    assert ob.suppress_publication(p, pending, lease=L(p), reason="pre-v2: destination unknown",
                                   now=NOW)["state"] == "SUPPRESSED"
    new = ob.enqueue_publication(p, lease=L(p), kind="OPS", purpose="new", body="n", chat_id=CHAT,
                                 now=NOW)["publication"]
    final = deliver(p, new["publication_id"], Fake(OK))  # message 42 may be the legacy row's message in this chat
    assert final["state"] == "UNKNOWN" and f"already belongs to {acked}" in events(p, new["publication_id"])[-1]["note"]


def test_23_invalid_transitions_are_refused(path):
    pid = rec(path, trade(path))["publication_id"]
    with pytest.raises(cj.InvalidTransition):
        ob.acknowledge_publication(path, pid, lease=L(path), message_id=5, now=NOW)
    with pytest.raises(cj.InvalidTransition):
        ob.resolve_unknown(path, pid, operator="skoust", delivered=False, evidence="x", now=NOW)
    ob.claim_publication(path, pid, lease=L(path), now=NOW)
    with pytest.raises(cj.InvalidTransition):
        ob.suppress_publication(path, pid, lease=L(path), reason="nope", now=NOW)
    with pytest.raises(cj.InvalidTransition):
        ob.claim_publication(path, pid, lease=L(path), now=NOW)
    conn = cj.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE crew_outbox SET state='PENDING', version=version+1")
    conn.close()
    acked = ob.acknowledge_publication(path, pid, lease=L(path), message_id=5, now=NOW)
    assert acked["state"] == "ACKED" and acked["acked_at"] == NOW.isoformat()
    assert ob.acknowledge_publication(path, pid, lease=L(path), message_id=5, now=NOW) == acked
    with pytest.raises(cj.InvalidTransition):
        ob.mark_unknown(path, pid, lease=L(path), reason="late", now=NOW)
    with pytest.raises(cj.InvalidTransition):
        ob.enqueue_publication(path, lease=L(path), kind="OPS", purpose="x", body="b", chat_id=CHAT, now=NOW.replace(tzinfo=None))
    with pytest.raises(cj.InvalidTransition, match="kind"):
        ob.enqueue_publication(path, lease=L(path), kind="ORDER", purpose="x", body="b", chat_id=CHAT, now=NOW)


def test_24_outbox_audit_is_append_only_and_lockstep(path):
    tid = trade(path)
    pid = rec(path, tid)["publication_id"]
    deliver(path, pid, Fake(OK))
    conn = cj.connect(path)
    for sql in ("UPDATE crew_outbox_events SET note='x'", "DELETE FROM crew_outbox_events",
                "DELETE FROM crew_outbox", "UPDATE crew_outbox SET body='edited'"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
    conn.close()
    seen = ob.inspect_publication(path, pid)
    assert [e["seq"] for e in seen["events"]] == [1, 2, 3] and seen["publication"]["version"] == 3
    assert cj.load_open_trades(path)[0]["execution_state"] == "NONE"


def test_25_tokens_are_redacted_everywhere(path, caplog):
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.INFO, logger="httpx")
    with pytest.raises(cj.InvalidTransition, match="credential"):
        ob.enqueue_publication(path, lease=L(path), kind="OPS", purpose="x", body=f"token bot{TOKEN}", chat_id=CHAT, now=NOW)
    pid = rec(path, trade(path))["publication_id"]
    leaked_desc = reply(400, {"ok": False, "error_code": 400, "description": f"bad url bot{TOKEN}/sendMessage"})
    out = http(raise_(httpx.ReadTimeout)).send("-100", "x")
    assert TOKEN not in (out.detail or "") and "[REDACTED]" in out.detail
    assert TOKEN not in classify_response(400, json.dumps({"ok": False, "error_code": 400,
                                                            "description": TOKEN}).encode()).detail
    deliver(path, pid, http(leaked_desc))
    logging.getLogger("httpx").info("HTTP Request: %s %s", "POST", f"https://api.telegram.org/bot{TOKEN}/sendMessage")
    assert TOKEN not in caplog.text and "[REDACTED]" in caplog.text
    for p in path.parent.glob("journal.db*"):
        assert TOKEN.encode() not in p.read_bytes()
    assert TOKEN not in json.dumps(ob.inspect_publication(path, pid))


def test_26_zero_real_network_and_no_broker_imports(path):
    deliver(path, rec(path, trade(path))["publication_id"], Fake(OK))
    deliver(path, ops(path)["publication_id"], http(reply(200, {"ok": True, "result": {"message_id": 99}})))
    assert NETWORK_CALLS == []
    for module in (ob, __import__("app.telegram_transport", fromlist=["x"])):
        imported = set()
        for node in ast.walk(ast.parse(Path(module.__file__).read_text())):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        assert not any(n and n.startswith(("app.brokers", "app.alerts", "app.trading.crew", "app.trading.scanner",
                                           "app.trading.orders", "app.trading.execution_crew")) for n in imported)


def test_27_production_files_are_unchanged(protected_file_hashes):
    assert hashlib.sha256(PROD_DB.read_bytes()).hexdigest() == protected_file_hashes[PROD_DB]
    assert hashlib.sha256(OCT30.read_bytes()).hexdigest() == protected_file_hashes[OCT30]


def test_production_outbox_connection_is_blocked(production_db_opens):
    with pytest.raises(RuntimeError, match="production database"):
        ob.enqueue_publication(PROD_DB, lease=cs.Lease("S1", DAY, 1, "unused"), kind="OPS", purpose="x", body="b",
                               chat_id=CHAT, now=NOW)
    production_db_opens.clear()
