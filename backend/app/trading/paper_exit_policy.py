"""Versioned paper-only liquidation policy; never an entry permission or broker order."""
import hashlib
import json
from pathlib import Path

LEGACY = "entry-spread-v1"
FRESH_BID = "fresh-bid-v1"
RULES = {
    LEGACY: {"version": LEGACY, "exit_spread": "same cap as entry"},
    FRESH_BID: {"version": FRESH_BID, "exit_spread": "entry cap does not prevent liquidation",
               "book": "fresh positive two-sided noninverted book, exact held contract",
               "min_bid": "0.05", "fill": "current bid minus existing adverse tick slippage",
               "session": "open market, unexpired contract", "unknown_exit": "retain exposure"},
}


def identity(policy):
    if policy not in RULES:
        raise ValueError("Unknown paper exit policy")
    # Legacy behavior remains pinned to its rules. New execution changes require fresh journals.
    files = ("option_quote.py", "paper_exec.py", "paper_exit_policy.py") if policy == FRESH_BID else ()
    source = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in files}
    return hashlib.sha256(json.dumps({"rules": RULES[policy], "source": source}, sort_keys=True).encode()).hexdigest()


def bind(conn, policy, *, trades_table):
    """Refuse policy changes or a new policy on an existing unbound trading history."""
    if trades_table not in ("crew_paper_trades", "trades"):
        raise ValueError("Unexpected paper ledger")
    digest = identity(policy)
    conn.execute("CREATE TABLE IF NOT EXISTS crew_paper_exit_identity "
                 "(id INTEGER PRIMARY KEY CHECK(id=1), policy TEXT NOT NULL, digest TEXT NOT NULL)")
    old = conn.execute("SELECT policy,digest FROM crew_paper_exit_identity WHERE id=1").fetchone()
    if old and tuple(old) != (policy, digest):
        raise ValueError("Paper exit policy/source changed; create a new journal")
    if not old:
        if policy != LEGACY and conn.execute(f"SELECT count(*) FROM {trades_table}").fetchone()[0]:
            raise ValueError("New paper exit policy requires a new journal")
        conn.execute("INSERT INTO crew_paper_exit_identity VALUES (1,?,?)", (policy, digest))
