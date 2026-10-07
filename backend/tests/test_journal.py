from __future__ import annotations


def test_journal_returns_sim_and_live_rows_with_shared_columns(client, live):
    sim = client.get("/api/journal", params={"kind": "sim", "limit": 1000}).json()["trades"]
    live_rows = client.get("/api/journal", params={"kind": "live"}).json()["trades"]
    assert sim and len(live_rows) == 1
    assert set(sim[0]) == set(live_rows[0])

    row = live_rows[0]
    assert row["trade_id"] == live["live_trade"].id
    assert row["reason_text"] == "IV rank 47 >= 40 with 12 DTE"
    assert row["trade_date"] == "2026-01-07"
    assert row["mode"] == "paper"
    assert row["outcome_label"] == "win"
    assert row["mistake_tags"] == ["seeded"]
    assert row["latest_verdict"] is None
    assert row["strategy_version"] == live["snapshot"].version

    assert all(r["latest_verdict"] for r in sim)
    assert all(isinstance(r["mistake_tags"], list) for r in sim)


def test_journal_filters(client, live):
    everything = client.get("/api/journal", params={"limit": 1000}).json()["trades"]
    assert {r["kind"] for r in everything} == {"sim", "live"}

    tagged = client.get("/api/journal", params={"tag": "high_cost_ratio", "limit": 1000}).json()["trades"]
    assert tagged and all("high_cost_ratio" in r["mistake_tags"] for r in tagged)

    wins = client.get("/api/journal", params={"outcome": "win", "limit": 1000}).json()["trades"]
    assert wins and all(r["outcome_label"] == "win" for r in wins)

    dated = client.get("/api/journal", params={"from": "2026-01-01", "to": "2026-12-31"}).json()["trades"]
    assert [r["kind"] for r in dated] == ["live"]

    client.post(f"/api/trades/live/{live['live_trade'].id}/reviews", json={"mistake_tags": ["exited_early"]})
    tagged_live = client.get("/api/journal", params={"tag": "exited_early"}).json()["trades"]
    assert [r["kind"] for r in tagged_live] == ["live"]

    page = client.get("/api/journal", params={"limit": 1}).json()["trades"]
    assert [r["kind"] for r in page] == ["live"]
    second = client.get("/api/journal", params={"limit": 1, "offset": 1}).json()["trades"]
    assert second and second[0]["kind"] == "sim"
