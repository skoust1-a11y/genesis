"""Configured credentials never imply delivery permission."""
import socket
from types import SimpleNamespace

import httpx
import pytest

from app import alerts
from app.config import get_settings
from app.telegram_transport import NOT_DELIVERED, TelegramTransport
from app.trading.crew.alert_agent import AlertAgent
from app.trading.post_market_crew.briefing_agent import BriefingAgent
from app.trading.pre_market_crew.pre_market_manager import PreMarketManager


def test_all_configured_senders_are_disabled(monkeypatch, tmp_path):
    def refuse(*args, **kwargs):
        pytest.fail("real Telegram sender reached HTTP")

    monkeypatch.setattr(httpx, "post", refuse)
    monkeypatch.setattr(httpx.Client, "post", refuse)
    monkeypatch.setattr("requests.post", refuse)
    settings = get_settings().model_copy(update={
        "alert_telegram_bot_token": "synthetic-token", "alert_telegram_chat_id": "123",
        "signals_telegram_chat_id": "456"})
    assert alerts.send(settings, "PAPER", "body") == []
    assert alerts.send_signal(settings, "PAPER", "body") == []
    assert alerts.send_telegram_signal(settings, "PAPER", reply_to_message_id=5) is None
    assert alerts.send_photo(settings, "PAPER", b"image") is None
    assert alerts.send_signal_photo(settings, "PAPER", b"image") is None
    assert alerts.send_video(settings, "PAPER", b"video") is False
    assert alerts.send_signal_video(settings, "PAPER", b"video") is False
    assert TelegramTransport("synthetic-token").send("123", "PAPER").status == NOT_DELIVERED
    assert not AlertAgent(bot_token="synthetic-token", chat_id="123")._send("PAPER")
    assert not BriefingAgent(bot_token="synthetic-token", chat_id="123", reports_dir=tmp_path)._send_telegram("PAPER")
    manager = object.__new__(PreMarketManager)
    manager.bot_token, manager.chat_id = "synthetic-token", "123"
    plan = SimpleNamespace(date="2026-11-06", trap_warning="", is_opening_trap=False,
        gap_bias="NEUTRAL", gap_points=0, expected_open=24000, pdc=24000, regime="UNKNOWN",
        call_wall=24100, put_wall=23900, max_pain=24000, pcr=1, approved_lots_standard=1,
        approved_lots_super=1, otm_steps=0, event_risk="UNKNOWN")
    assert not manager.dispatch_telegram_battleplan(plan)


def test_mock_destination_never_reaches_even_mock_http():
    def refuse(request):
        pytest.fail("mock destination reached HTTP")

    with httpx.Client(transport=httpx.MockTransport(refuse)) as client:
        assert TelegramTransport("synthetic-token", client=client).send("mock:paper", "PAPER").status == NOT_DELIVERED


def test_mock_client_cannot_hide_real_mount():
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200)),
                      mounts={"https://api.telegram.org": httpx.HTTPTransport()}) as client:
        result = TelegramTransport("synthetic-token", client=client).send("123", "PAPER")
        assert result.status == NOT_DELIVERED and result.exc_class == "DeliveryDisabled"


@pytest.mark.parametrize("operation", ["dns", "connect", "udp"])
def test_outbound_network_is_blocked(operation, outbound_attempts):
    try:
        with pytest.raises(RuntimeError, match="Offline tests"):
            if operation == "dns":
                socket.getaddrinfo("example.invalid", 443)
            elif operation == "connect":
                with socket.socket() as sock:
                    sock.connect(("192.0.2.1", 443))
            else:
                with socket.socket(type=socket.SOCK_DGRAM) as sock:
                    sock.sendto(b"test", ("192.0.2.1", 443))
        assert outbound_attempts
    finally:
        outbound_attempts.clear()


def test_loopback_server_remains_available():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=1) as client:
            connection, _ = server.accept()
            with connection:
                client.sendall(b"local")
                assert connection.recv(5) == b"local"
