"""B4/B5 contracts using explicit temporary state; no production-state inspection."""
from decimal import Decimal

from app.config import Settings
from app.db import trade_store
from app.trading.crew.risk_agent import RiskAgent
from app.trading.post_market_crew.safety_wall_agent import SafetyWallAgent
from app.trading.paper_accounting import stop_budget
from app.trading.walk_forward import DEFAULT_INITIAL_CAPITAL_INR


def test_explicit_state_paths_are_cwd_invariant(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    risk_path = state / "risk.json"
    risk_path.write_text('{"base_capital":50000,"safety_wall_reserve":123}')
    wall_path = state / "wall.json"
    wall_path.write_text('{"reserve_balance":123}')
    db_path = state / "trades.db"
    with trade_store.get_connection(db_path) as conn:
        conn.execute("CREATE TABLE marker (value TEXT)")
        conn.execute("INSERT INTO marker VALUES ('temporary')")
    for name in ("root", "backend", "elsewhere"):
        cwd = tmp_path / name
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        risk = RiskAgent(config_path=risk_path)
        wall = SafetyWallAgent(state_file=wall_path, config_file=risk_path)
        assert risk.base_capital == 50000
        assert risk.safety_wall_reserve == wall.reserve_balance == 123
        with trade_store.get_readonly_connection(db_path) as conn:
            assert conn.execute("SELECT value FROM marker").fetchone()[0] == "temporary"
        assert not (cwd / "trades").exists()
        assert not (cwd / "data").exists()


def test_capital_authorities_remain_separate(monkeypatch):
    for name in ("DEFAULT_CAPITAL_INR", "PAPER_CAPITAL_INR"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert DEFAULT_INITIAL_CAPITAL_INR == 50000
    assert settings.default_capital_inr == 500000
    assert settings.paper_capital_inr is None
    monkeypatch.setenv("DEFAULT_CAPITAL_INR", "75000")
    assert Settings(_env_file=None).paper_capital_inr is None
    monkeypatch.setenv("PAPER_CAPITAL_INR", "50000")
    settings = Settings(_env_file=None)
    assert settings.default_capital_inr == 75000
    assert settings.paper_capital_inr == Decimal("50000")


def test_effective_capital_budget_does_not_compound():
    assert stop_budget(Decimal("60000"), Decimal("50000")) == Decimal("1000.00")
    assert stop_budget(Decimal("40000"), Decimal("50000")) == Decimal("800.00")
