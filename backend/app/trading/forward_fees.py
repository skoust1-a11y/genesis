"""Daily evidenced Upstox paper tariff. Never extends a historical policy's dates."""
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from app.trading.option_quote import IST

DIRECTORY = Path(__file__).resolve().parents[3] / "logs/paper-forward/fees"
URL = "https://upstox.com/brokerage-charges/"
PREFIX = "nse-index-option-upstox-paper-"
# The exchange+IPFT total is 0.03553%; split the included Rs 0.01/crore contribution.
RATES = {"brokerage_per_order": "20.00", "stt_sell": "0.0015", "exchange": "0.000355299",
         "ipft": "0.000000001", "sebi": "0.000001", "stamp_buy": "0.00003", "gst": "0.18"}


def validate_tariff(html):
    rows = {}
    for tr in BeautifulSoup(html, "html.parser").select("tr"):
        cells = [" ".join(c.get_text(" ", strip=True).split()) for c in tr.select("td, th")]
        if len(cells) == 5:
            rows[cells[0]] = cells[-1]
    expected = {"Brokerage": "Flat ₹20 per executed order", "STT/CTT (From 1st April 2026)": "0.15% on sell side (on premium)",
                "GST": "18% (on brokerage + transaction charges + IPFT charges)", "Stamp Duty": "0.003% or ₹300 / crore on buy side",
                "Transaction charges (From 1st March 2026)": "NSE: 0.03553% (on premium)",
                "IPFT": "₹0.01 per crore on Traded value (On Premium) + GST", "SEBI Charges": "₹10/crore"}
    for label, prefix in expected.items():
        if not rows.get(label, "").startswith(prefix):
            raise ValueError("PUBLISHED_TARIFF_CHANGED_OR_INCOMPLETE")
    known = {"STT/CTT (From 1st October 2024 to 31st March 2026)", "STT/CTT (From 1st April 2026)",
             "Transaction charges (From 1st March 2026)", "Transaction charges (From 1st October 2024 to 28 February 2026)"}
    if any(label.startswith(("STT/CTT", "Transaction charges")) and label not in known for label in rows):
        raise ValueError("PUBLISHED_TARIFF_EFFECTIVE_DATE_CHANGED")
    return dict(RATES)


def attest(*, now=None):
    now = now or datetime.now(IST)
    day = now.astimezone(IST).date().isoformat()
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        response = client.get(URL)
        response.raise_for_status()
    rates = validate_tariff(response.text)
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    evidence = DIRECTORY / (day + ".html")
    # Each date's first successful attestation is immutable; changed rates require code review.
    if not (DIRECTORY / (day + ".json")).exists():
        evidence.write_bytes(response.content)
        data = {"market_day": day, "checked_at": now.isoformat(), "rates": rates, "source": URL,
                "source_sha256": hashlib.sha256(response.content).hexdigest()}
        temp = DIRECTORY / (day + ".json.tmp")
        temp.write_text(json.dumps(data, indent=2) + "\n")
        temp.replace(DIRECTORY / (day + ".json"))
    return policy(day)


def policy(day, policy_id=None):
    from app.trading.paper_fees import FeePolicySpec
    expected_id = PREFIX + day + "-v1"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or (policy_id is not None and policy_id != expected_id):
        return None
    try:
        data = json.loads((DIRECTORY / (day + ".json")).read_text())
        body = (DIRECTORY / (day + ".html")).read_bytes()
        if (data["market_day"] != day or datetime.fromisoformat(data["checked_at"]).astimezone(IST).date().isoformat() != day
                or data["rates"] != RATES or data["source"] != URL or hashlib.sha256(body).hexdigest() != data["source_sha256"]):
            return None
        validate_tariff(body.decode())
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return FeePolicySpec(expected_id, day, day, dict(RATES), (URL, "https://nsearchives.nseindia.com/content/circulars/FATAX73524.pdf"))


def from_id(policy_id):
    match = re.fullmatch(re.escape(PREFIX) + r"(\d{4}-\d{2}-\d{2})-v1", str(policy_id))
    return policy(match[1], policy_id) if match else None
