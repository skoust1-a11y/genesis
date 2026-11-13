"""Paper group delivery uses explicit authorization and durable acknowledgement."""
from types import SimpleNamespace
from unittest.mock import Mock

import httpx

from app.config import get_settings
from app.telegram_transport import ARM_ENV, DELIVERED, NOT_DELIVERED, TelegramTransport, delivery_authorized
from app.trading import paper_delivery as pd


def configure(monkeypatch, enabled="true", armed="1"):
    monkeypatch.setenv("TELEGRAM_DELIVERY_ENABLED", enabled)
    monkeypatch.setenv("SIGNALS_TELEGRAM_CHAT_ID", "-100123")
    monkeypatch.setenv("ALERT_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv(ARM_ENV, armed)
    get_settings.cache_clear()
    return get_settings()


def test_real_authorization_is_limited_to_the_enabled_group(monkeypatch):
    configure(monkeypatch)
    assert delivery_authorized("-100123")
    assert not delivery_authorized("-999")
    assert not delivery_authorized("mock:paper")
    configure(monkeypatch, "false")
    assert not delivery_authorized("-100123")


def test_persistent_enablement_alone_never_sends(monkeypatch, tmp_path):
    for armed in ("", "0", "true", "yes", " 1"):
        configure(monkeypatch, armed=armed)
        assert not delivery_authorized("-100123")
    monkeypatch.delenv(ARM_ENV)
    assert not delivery_authorized("-100123")
    # A .env line cannot arm the process: pydantic reads it into Settings, not os.environ.
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"{ARM_ENV}=1\nTELEGRAM_DELIVERY_ENABLED=true\n")
    get_settings.cache_clear()
    assert not delivery_authorized("-100123")
    client = Mock()
    monkeypatch.setattr(httpx, "Client", lambda: client)
    outcome = TelegramTransport("test-token").send("-100123", "PAPER ONLY")
    assert outcome.status == NOT_DELIVERED and outcome.exc_class == "DeliveryDisabled"
    client.post.assert_not_called()


def test_transport_creates_and_closes_its_real_client(monkeypatch):
    configure(monkeypatch)
    client = Mock()
    client.post.return_value = httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
    monkeypatch.setattr(httpx, "Client", lambda: client)
    result = TelegramTransport("test-token").send("-100123", "PAPER ONLY")
    assert result.status == DELIVERED and result.message_id == 7
    client.close.assert_called_once()
    assert client.post.call_args.kwargs["json"]["chat_id"] == "-100123"


def test_dispatch_only_delivers_paper_rows_to_the_configured_group(monkeypatch):
    settings = configure(monkeypatch)
    base = {"kind": "OPS", "purpose": "paper|ENTRY|one", "chat_id": "-100123", "publication_id": "one"}
    rows = [base, dict(base, chat_id="mock:paper"), dict(base, purpose="other"), dict(base, kind="RECOMMENDATION")]
    monkeypatch.setattr(pd.ob, "list_pending_publications", lambda *a, **k: rows)
    deliver = Mock(return_value={"state": "ACKED"})
    monkeypatch.setattr(pd.ob, "deliver_publication", deliver)
    clock = Mock()
    assert pd.dispatch("unused", lease=object(), settings=settings, clock=clock) == [{"state": "ACKED"}]
    deliver.assert_called_once()
    configure(monkeypatch, "false")
    assert pd.dispatch("unused", lease=object(), settings=get_settings(), clock=clock) == []


def test_unpriced_paper_entry_is_reported_without_invented_premiums(monkeypatch):
    from app.trading.crew.alert_agent import AlertAgent
    agent = AlertAgent(is_paper_trading=True)
    send = Mock(return_value=True)
    monkeypatch.setattr(agent, "_send", send)
    signal = SimpleNamespace(strategy="sweep", suggested_strike="NIFTY CE", direction="CALL",
                             entry_price=24000, stop_loss=23950, target_price=24100)
    assert agent.send_unpriced_paper_entry(signal)
    assert "UNPRICED" in send.call_args.args[0]
    assert "Option premium unavailable" in send.call_args.args[0]
