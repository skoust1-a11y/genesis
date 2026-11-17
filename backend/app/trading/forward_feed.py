"""Operational read-only feeds composed around the unchanged scanner rules."""
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx

from app.brokers.upstox_stream import UpstoxStream
from app.market.news import parse_feed
from app.market.store import open_market_store
from app.trading.market_validation import _start
from app.trading.option_quote import IST
from app.trading.research_runtime import scanner_feed
from app.trading.scanner_crew.feed_agent import FeedAgent
from app.trading.scanner_crew.scanner_manager import ScannerManager


class OperationalFeedAgent(FeedAgent):
    """Separate Yahoo's trailing current-price print from its OHLC candle rows."""

    def __init__(self):
        super().__init__()
        self.live_prints = {}

    def fetch_timeframe_candles(self, timeframe="5m", limit_bars=50, mock_df=None):
        frame = super().fetch_timeframe_candles(timeframe, limit_bars, mock_df)
        self.live_prints.pop(timeframe, None)
        return self.candle_rows(frame, timeframe, datetime.now(IST))

    def candle_rows(self, frame, timeframe, now):
        if timeframe not in ("5m", "15m") or len(frame) < 2:
            return frame
        try:
            row = frame.iloc[-1]
            stamp = _start(row)
            previous = _start(frame.iloc[-2])
            clock = now.astimezone(IST).replace(tzinfo=None)
            minutes = int(timeframe[:-1])
            bucket = clock.replace(minute=clock.minute // minutes * minutes, second=0, microsecond=0)
            # Do not drop any historical row, completed-bar anomaly, or actual OHLC range.
            if (stamp.minute % minutes and stamp.date() == clock.date()
                    and bucket <= stamp <= clock and previous < stamp
                    and len({float(row[k]) for k in ("open", "high", "low", "close")}) == 1
                    and float(row.get("volume", 0) or 0) == 0):
                self.live_prints[timeframe] = row.to_dict()
                return frame.iloc[:-1].copy()
        except (TypeError, ValueError, KeyError):
            pass  # Leave malformed data intact for the strict validator to reject.
        return frame


class ForwardInputs:
    def __init__(self, config, token):
        self.config = config
        self.stream = UpstoxStream(token, authorized=config.upstox_market_data_authorized)
        self.scanner = ScannerManager(feed_agent=OperationalFeedAgent())
        self.feed = scanner_feed(scanner=self.scanner, chain_source=self._baseline_chain)
        self.stop_event = threading.Event()
        self.news = {"headlines": 0, "state": "NOT_FETCHED"}
        self.headlines = []
        self.news_thread = None
        self.last_observation = None
        self.profit_watch_thread = None
        self.profit_watch_error = None

    def _baseline_chain(self):
        """Adapt the provider schema to the unchanged baseline scanner contract."""
        page = self.stream.chain()
        chain = page.get("optionChain") or {}
        return {"optionChain": {"optionContracts": chain.get("contracts") or [],
                                "aggregatedDetails": chain.get("aggregatedDetails") or {}}}

    def start(self):
        self.stream.start()
        self.news_thread = threading.Thread(target=self._news_loop, name="paper-news", daemon=True)
        self.news_thread.start()

    def close(self):
        self.stop_event.set()
        if self.profit_watch_thread:
            self.profit_watch_thread.join(timeout=6)
            if self.profit_watch_thread.is_alive():
                raise RuntimeError("Paper profit monitor did not stop")
        self.stream.close()
        if self.news_thread:
            self.news_thread.join(timeout=1)

    def refresh_news(self):
        now = datetime.now(UTC)
        items, sources = [], {}
        with httpx.Client(timeout=8, follow_redirects=True, headers={"User-Agent": "GenesisPaper/1.0"}) as client:
            for name, url in self.config.news_feed_urls().items():
                try:
                    response = client.get(url)
                    response.raise_for_status()
                    parsed = parse_feed(response.text, name, now)
                    items.extend(parsed)
                    sources[name] = len(parsed)
                except Exception as exc:  # noqa: BLE001 - isolate unavailable feeds and malformed provider XML
                    sources[name] = type(exc).__name__
        store = open_market_store(self.config)
        try:
            added = store.add_news(items)
            recent = store.news_between(now - timedelta(hours=self.config.news_lookback_hours), now, self.config.news_max_items)
            self.headlines = [{"title": n.title, "published_at": n.published_at.isoformat(), "source": n.source} for n in recent]
        finally:
            store.close()
        self.news = {"headlines": len(self.headlines), "updated_at": now.isoformat(), "sources": sources,
                     "added": added, "state": "AVAILABLE" if self.headlines else "NO_RECENT_HEADLINES"}

    def _news_loop(self):
        while not self.stop_event.is_set():
            try:
                self.refresh_news()
            except Exception as exc:  # noqa: BLE001 - collector failure must block entries, not terminate exit monitoring
                self.news = {"headlines": 0, "state": type(exc).__name__}
                self.headlines = []
            self.stop_event.wait(300)

    def __call__(self, now):
        # Expire the cache even if every subsequent refresh fails.
        cutoff = now - timedelta(hours=self.config.news_lookback_hours)
        headlines = [h for h in self.headlines if cutoff < datetime.fromisoformat(h["published_at"]) <= now]
        self.scanner.news_agent.cached_headlines = headlines
        view = self.feed(now)
        view = replace(view, delta_evidence=self.stream.evidence())
        if not view.df_5m.empty and not view.rejections and view.delta_evidence and headlines:
            self.last_observation = {"market_day": now.date().isoformat(), "observed_at": now.isoformat(),
                                     "source": "upstox_v3_full_live_snapshot", "valid_closed_bars": len(view.df_5m),
                                     "fresh_snapshot_contracts": len(view.delta_evidence), "recent_headlines": len(headlines)}
        if not headlines:
            # ResearchRuntime still monitors existing exits, but new admissions stop.
            view = replace(view, setups=[], rejections={**view.rejections, "5m": ["RECENT_NEWS_UNAVAILABLE"]})
        return view

    @property
    def sources(self):
        return {"option_chain": "upstox_v3_full_live_snapshot", "index_bars": "yahoo_chart",
                "delta": "upstox_v3_full_live_snapshot", "news": "configured RSS feeds"}
