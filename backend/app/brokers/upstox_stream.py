"""Read-only V3 full-feed snapshots for paper valuation. Never calls an order endpoint.

Schema: https://assets.upstox.com/feed/market-data-feed/v3/MarketDataFeed.proto
Only used wire fields are decoded; protobuf preserves/skips the other fields.
currentTs dates the full live snapshot, not the last trade or local fetch time.
Initial snapshots, closed markets, future/stale ticks and partial messages are refused.
"""
from __future__ import annotations

import json
import logging
import math
import threading
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlparse

import httpx
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from app.brokers.upstox_api import UpstoxMarketData
from app.trading.option_quote import IST
from app.trading.paper_risk import DeltaEvidence

SOURCE = "upstox_v3_full_live_snapshot"
log = logging.getLogger(__name__)


def response_type():
    f = descriptor_pb2.FileDescriptorProto(name="paper_upstox_v3.proto", package="paperupstox", syntax="proto3")
    double, int64, string, message, int32 = 1, 3, 9, 11, 5
    definitions = {
        "LTPC": [("ltp", 1, double, None)],
        "Quote": [("bidQ", 1, int64, None), ("bidP", 2, double, None), ("askQ", 3, int64, None), ("askP", 4, double, None)],
        "MarketLevel": [("bidAskQuote", 1, message, "Quote")],
        "OptionGreeks": [("delta", 1, double, None)],
        "MarketFullFeed": [("ltpc", 1, message, "LTPC"), ("marketLevel", 2, message, "MarketLevel"), ("optionGreeks", 3, message, "OptionGreeks")],
        "FullFeed": [("marketFF", 1, message, "MarketFullFeed")],
        "Feed": [("fullFeed", 2, message, "FullFeed")],
        "MarketInfo": [],
        "FeedResponse": [("type", 1, int32, None), ("currentTs", 3, int64, None), ("marketInfo", 4, message, "MarketInfo")],
    }
    for name, fields in definitions.items():
        m = f.message_type.add(name=name)
        for fname, number, kind, target in fields:
            field = m.field.add(name=fname, number=number, type=kind, label=3 if name == "MarketLevel" else 1)
            if target:
                field.type_name = ".paperupstox." + target
        if name in ("FeedResponse", "MarketInfo"):
            entry = m.nested_type.add(name="Entry")
            entry.options.map_entry = True
            entry.field.add(name="key", number=1, type=string, label=1)
            value = entry.field.add(name="value", number=2, type=message if name == "FeedResponse" else int32, label=1)
            if name == "FeedResponse":
                value.type_name = ".paperupstox.Feed"
            m.field.add(name="feeds" if name == "FeedResponse" else "segmentStatus", number=2 if name == "FeedResponse" else 1,
                        type=message, type_name=f".paperupstox.{name}.Entry", label=3)
    pool = descriptor_pool.DescriptorPool()
    pool.Add(f)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName("paperupstox.FeedResponse"))


Response = response_type()


class UpstoxStream:
    def __init__(self, token, *, authorized, clock=lambda: datetime.now(UTC)):
        self.reader = UpstoxMarketData(token, authorized=authorized, clock=clock)
        self.clock = clock
        self.contracts = {}
        self.expiry = None
        self.cache = {}
        self.market_open = False
        self.state = "NOT_CONNECTED"
        self.last_tick = None
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None

    def prepare(self):
        contracts = self.reader.contracts("NSE_INDEX|Nifty 50")
        if any(c["underlying_symbol"] != "NIFTY" for c in contracts.values()):
            raise ValueError("STREAM_UNDERLYING_IDENTITY_MISMATCH")
        today = self.clock().astimezone(IST).date().isoformat()
        expiries = sorted({c["expiry"] for c in contracts.values() if c["expiry"] >= today})
        if not expiries:
            raise ValueError("NO_UNEXPIRED_CONTRACTS")
        self.expiry = expiries[0]
        self.contracts = {k: c for k, c in contracts.items() if c["expiry"] == self.expiry}
        if not 0 < len(self.contracts) <= 1000:
            raise ValueError("STREAM_SUBSCRIPTION_LIMIT")

    def accept(self, raw):
        packet = Response.FromString(raw)
        if packet.HasField("marketInfo"):
            self.market_open = packet.marketInfo.segmentStatus.get("NSE_FO") == 2
        now = self.clock()
        try:
            stamp = datetime.fromtimestamp(packet.currentTs / 1000, UTC)
        except (ValueError, OverflowError, OSError):
            return
        if packet.type != 1 or not self.market_open or not 0 <= (now - stamp).total_seconds() <= 10:
            return
        with self.lock:
            for key, feed in packet.feeds.items():
                contract = self.contracts.get(key)
                if contract is None or not feed.HasField("fullFeed") or not feed.fullFeed.HasField("marketFF"):
                    continue
                full = feed.fullFeed.marketFF
                if not full.HasField("marketLevel") or not full.HasField("optionGreeks"):
                    continue
                bids = [q.bidP for q in full.marketLevel.bidAskQuote if q.bidQ > 0 and q.bidP > 0]
                asks = [q.askP for q in full.marketLevel.bidAskQuote if q.askQ > 0 and q.askP > 0]
                delta = full.optionGreeks.delta
                if not bids or not asks or not math.isfinite(delta):
                    continue
                bid, ask = max(bids), min(asks)
                valid_delta = 0 < delta <= 1 if contract["instrument_type"] == "CE" else -1 <= delta < 0
                if not valid_delta or not all(math.isfinite(x) for x in (bid, ask)) or bid > ask:
                    continue
                prior = self.cache.get(key)
                if prior and stamp <= prior["stamp"]:
                    continue
                self.cache[key] = {"stamp": stamp, "bid": bid, "ask": ask, "delta": delta, "ltp": full.ltpc.ltp}
                self.last_tick = stamp.isoformat()
                self.state = "LIVE_SNAPSHOT_RECEIVED"

    def chain(self):
        rows = {}
        now = self.clock()
        with self.lock:
            for key, contract in self.contracts.items():
                strike = float(contract["strike_price"])
                row = rows.setdefault(strike, {"underlying": "NIFTY", "strikePrice": strike, "lotSize": contract["lot_size"]})
                leg = {"growwContractId": key, "tradingSymbol": contract["trading_symbol"], "marketLot": contract["lot_size"]}
                tick = self.cache.get(key)
                if self.market_open and tick and 0 <= (now - tick["stamp"]).total_seconds() <= 10:
                    leg["liveData"] = {"bid": tick["bid"], "ask": tick["ask"], "ltp": tick["ltp"], "quote_time": tick["stamp"].isoformat()}
                    leg["greeks"] = {"delta": tick["delta"]}
                row[contract["instrument_type"].lower()] = leg
        return {"optionChain": {"contracts": list(rows.values()), "aggregatedDetails": {"currentExpiry": self.expiry}}}

    def evidence(self):
        now = self.clock()
        with self.lock:
            return {self.contracts[k]["trading_symbol"]: DeltaEvidence(Decimal(str(v["delta"])), v["stamp"], self.contracts[k]["trading_symbol"], SOURCE)
                    for k, v in self.cache.items() if self.market_open and 0 <= (now - v["stamp"]).total_seconds() <= 10}

    def start(self):
        self.prepare()
        self.thread = threading.Thread(target=self._run, daemon=True, name="upstox-read-only")
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=8)

    def _run(self):
        from websockets.sync.client import connect

        backoff_seconds = 2
        while not self.stop_event.is_set():
            received_live = False
            try:
                with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
                    response = client.get("https://api.upstox.com/v3/feed/market-data-feed/authorize",
                                          headers={"Authorization": "Bearer " + self.reader._token.get_secret_value(), "Accept": "application/json"})
                    response.raise_for_status()
                    url = response.json()["data"]["authorized_redirect_uri"]
                parsed = urlparse(url)
                if parsed.scheme != "wss" or not parsed.hostname or not parsed.hostname.endswith(".upstox.com"):
                    raise ValueError("STREAM_HOST_REFUSED")
                with connect(url, open_timeout=15, close_timeout=3, ping_interval=20, ping_timeout=15) as socket:
                    self.state = "CONNECTED_WAITING_FOR_LIVE_DATA"
                    socket.send(json.dumps({"guid": "genesis-paper", "method": "sub", "data": {"mode": "full", "instrumentKeys": list(self.contracts)}}).encode())
                    while not self.stop_event.is_set():
                        try:
                            raw = socket.recv(timeout=5)
                        except TimeoutError:
                            continue
                        if isinstance(raw, bytes):
                            self.accept(raw)
                            if not received_live and self.state == "LIVE_SNAPSHOT_RECEIVED":
                                received_live = True
                                backoff_seconds = 2
            except Exception as exc:  # noqa: BLE001 - reconnect after provider/decoder failures without logging secret URLs
                # Authorized websocket URLs contain a one-use secret; never log exceptions/URLs.
                log.warning("Read-only stream disconnected (%s); reconnecting in %ds", type(exc).__name__, backoff_seconds)
                self.state = "DISCONNECTED"
            finally:
                self.market_open = False
                with self.lock:
                    self.cache.clear()
            self.stop_event.wait(backoff_seconds)
            backoff_seconds = min(30, backoff_seconds * 2)
