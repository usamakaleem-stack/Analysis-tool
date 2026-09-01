#!/usr/bin/env python3
"""
Fetches the live PSX price and writes data.json for the PSX Fair Value
Ledger dashboard (index.html).

Place this file at:  scripts/fetch_psx_data.py   (in your repo)
Run it with:          python scripts/fetch_psx_data.py
It writes:            data.json   (next to index.html, at the repo root)

DESIGN
------
Only PRICE is fetched live. Everything else the dashboard needs —
EPS, ROE, P/E, P/B, D/E, dividend yield — is plain arithmetic on top of
Net Income, Equity, Shares Outstanding, Total Debt, and the last-declared
dividend per share. Those five inputs only change once a year (or once a
quarter) when a company actually reports earnings, so they live in
STATIC_FUNDAMENTALS below as hand-entered numbers, refreshed a few times a
year from each company's financial statements — not scraped.

This intentionally replaces an earlier version of this script that tried to
scrape P/E, EPS, ROE etc. directly from a third-party ratios page. That
approach turned out to be fragile in practice (the page's table structure
didn't match what was assumed) and was solving a problem that didn't need
solving: those ratios are derivable, not something that needs a live feed.

DATA SOURCE FOR LIVE PRICE
--------------------------
scstrade.com's company snapshot page:
  https://www.scstrade.com/stockscreening/SS_CompanySnapShot.aspx?symbol=<TICKER>
Confirmed working in a real GitHub Actions run (2026-09-01) — the price for
all six configured stocks was fetched successfully.

WHAT STILL NEEDS VERIFYING
---------------------------
fetch_eod_history() below, for the 1W/1M/5Y price trend charts, uses PSX's
own timeseries endpoint. Its exact response shape was not confirmed against
a live response — check the Action's log for "EOD response shape
unexpected" and adjust that function if you see it.

ROBUSTNESS
----------
If the live price fetch fails for a symbol, the script keeps that symbol's
previous price from data.json rather than writing a blank or wrong number.
"""

import json
import os
import re
import sys
import time
import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; psx-fair-value-ledger/1.0; personal use)"}
REQUEST_TIMEOUT = 15
POLITE_DELAY_SECONDS = 1.5
DEBUG = os.environ.get("PSX_DEBUG") == "1"

OUT_PATH = Path(__file__).resolve().parent.parent / "data.json"

# ---------------------------------------------------------------------------
# Sector benchmarks (used for the "vs sector average" comparisons).
# ---------------------------------------------------------------------------
BANK_DE_NOTE = (
    "Leverage ratios work differently for banks — deposits count as "
    "liabilities, so D/E isn't a useful safety signal here. Capital "
    "adequacy is the better check."
)

STOCK_CONFIG = {
    "OGDC": {"name": "Oil & Gas Development Co.", "sector": "Oil & Gas E&P",
             "sectorPE": 7.5, "sectorPB": 1.3, "sectorROE": 13, "sectorDE": 0.30, "deNote": None},
    "LUCK": {"name": "Lucky Cement Ltd.", "sector": "Cement",
             "sectorPE": 9.0, "sectorPB": 2.0, "sectorROE": 20, "sectorDE": 0.60, "deNote": None},
    "HBL":  {"name": "Habib Bank Ltd.", "sector": "Commercial Banking",
             "sectorPE": 6.5, "sectorPB": 1.1, "sectorROE": 16, "sectorDE": None, "deNote": BANK_DE_NOTE},
    "FFC":  {"name": "Fauji Fertilizer Co.", "sector": "Fertilizer",
             "sectorPE": 8.5, "sectorPB": 3.0, "sectorROE": 32, "sectorDE": 0.40, "deNote": None},
    "MEBL": {"name": "Meezan Bank Ltd.", "sector": "Islamic Banking",
             "sectorPE": 6.5, "sectorPB": 1.1, "sectorROE": 16, "sectorDE": None, "deNote": BANK_DE_NOTE},
    "UBL":  {"name": "United Bank Ltd.", "sector": "Commercial Banking",
             "sectorPE": 6.5, "sectorPB": 1.1, "sectorROE": 16, "sectorDE": None, "deNote": BANK_DE_NOTE},
}

# ---------------------------------------------------------------------------
# HAND-MAINTAINED fundamentals. Refresh from each company's latest annual /
# quarterly report — a few times a year, not live. Units: netIncome and
# equity and totalDebt are in PKR billions; shares is in billions of shares;
# dividendPerShare is in PKR per share (the last declared annual dividend).
# totalDebt is null for banks — D/E isn't computed for them (see BANK_DE_NOTE).
# ---------------------------------------------------------------------------
STATIC_FUNDAMENTALS = {
    "OGDC": {"netIncome": 155.56, "equity": 1370.6, "shares": 4.30,  "totalDebt": 0.0,   "dividendPerShare": 15.05},
    "LUCK": {"netIncome": 76.96,  "equity": 318.0,  "shares": 1.47,  "totalDebt": 152.6, "dividendPerShare": 4.03},
    "HBL":  {"netIncome": 65.71,  "equity": 436.4,  "shares": 1.47,  "totalDebt": None,  "dividendPerShare": 20.04},
    "FFC":  {"netIncome": 85.36,  "equity": 248.4,  "shares": 1.39,  "totalDebt": 62.1,  "dividendPerShare": 40.60},
    "MEBL": {"netIncome": 90.93,  "equity": 266.6,  "shares": 1.812, "totalDebt": None,  "dividendPerShare": 27.04},
    "UBL":  {"netIncome": 126.12, "equity": 383.6,  "shares": 2.480, "totalDebt": None,  "dividendPerShare": 31.97},
}

# Manually curated 5-yr revenue/opex (PKR bn). Refresh by hand once or twice
# a year — see the module docstring for why this isn't scraped.
STATIC_FINANCIALS = {
    "OGDC": [{"year": 2021, "revenue": 210, "opex": 160, "netIncome": 50},
             {"year": 2022, "revenue": 290, "opex": 205, "netIncome": 85},
             {"year": 2023, "revenue": 330, "opex": 230, "netIncome": 100},
             {"year": 2024, "revenue": 365, "opex": 250, "netIncome": 115},
             {"year": 2025, "revenue": 390.4, "opex": 234.8, "netIncome": 155.56}],
    "LUCK": [{"year": 2021, "revenue": 180, "opex": 160, "netIncome": 20},
             {"year": 2022, "revenue": 230, "opex": 212, "netIncome": 18},
             {"year": 2023, "revenue": 310, "opex": 278, "netIncome": 32},
             {"year": 2024, "revenue": 411.0, "opex": 345.4, "netIncome": 65.57},
             {"year": 2025, "revenue": 449.63, "opex": 372.67, "netIncome": 76.96}],
    "HBL":  [{"year": 2021, "revenue": 180, "opex": 155, "netIncome": 25},
             {"year": 2022, "revenue": 220, "opex": 185, "netIncome": 35},
             {"year": 2023, "revenue": 270, "opex": 225, "netIncome": 45},
             {"year": 2024, "revenue": 315.53, "opex": 257.73, "netIncome": 57.80},
             {"year": 2025, "revenue": 353.73, "opex": 288.02, "netIncome": 65.71}],
    "FFC":  [{"year": 2021, "revenue": 220, "opex": 175, "netIncome": 45},
             {"year": 2022, "revenue": 280, "opex": 215, "netIncome": 65},
             {"year": 2023, "revenue": 340, "opex": 270, "netIncome": 70},
             {"year": 2024, "revenue": 411.25, "opex": 326.88, "netIncome": 84.37},
             {"year": 2025, "revenue": 517.66, "opex": 432.30, "netIncome": 85.36}],
    "MEBL": [{"year": 2021, "revenue": 190, "opex": 145, "netIncome": 45},
              {"year": 2022, "revenue": 225, "opex": 163, "netIncome": 62},
              {"year": 2023, "revenue": 265, "opex": 183, "netIncome": 82},
              {"year": 2024, "revenue": 309.97, "opex": 208.47, "netIncome": 101.50},
              {"year": 2025, "revenue": 291.22, "opex": 200.29, "netIncome": 90.93}],
    "UBL":  [{"year": 2021, "revenue": 104.5, "opex": 77.5, "netIncome": 27},
             {"year": 2022, "revenue": 138.8, "opex": 93.8, "netIncome": 45},
             {"year": 2023, "revenue": 184.3, "opex": 119.3, "netIncome": 65},
             {"year": 2024, "revenue": 244.47, "opex": 163.95, "netIncome": 80.52},
             {"year": 2025, "revenue": 428.91, "opex": 302.79, "netIncome": 126.12}],
}


def log(msg):
    print(f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Live price
# ---------------------------------------------------------------------------
def fetch_price(symbol):
    """Returns the current price as a float, or None on failure. Confirmed
    working against scstrade.com's snapshot page in a live Actions run."""
    url = f"https://www.scstrade.com/stockscreening/SS_CompanySnapShot.aspx?symbol={symbol.lower()}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        log(f"{symbol}: price fetch failed: {e}")
        return None

    text = BeautifulSoup(r.text, "html.parser").get_text()
    m = re.search(r"Rs\.\s*([\d,]+\.\d+)", text)
    if not m:
        log(f"{symbol}: could not find a price on the page")
        return None
    return float(m.group(1).replace(",", ""))


# ---------------------------------------------------------------------------
# Computed fundamentals (arithmetic, not scraped)
# ---------------------------------------------------------------------------
def compute_fundamentals(symbol, price):
    f = STATIC_FUNDAMENTALS[symbol]
    eps = f["netIncome"] / f["shares"]
    bvps = f["equity"] / f["shares"]
    roe = (f["netIncome"] / f["equity"]) * 100
    de = round(f["totalDebt"] / f["equity"], 4) if f["totalDebt"] is not None else None
    result = {"eps": round(eps, 2), "bvps": round(bvps, 2), "roe": round(roe, 2), "de": de}
    if price:
        result["pe"] = round(price / eps, 2)
        result["pb"] = round(price / bvps, 2)
        result["divYield"] = round((f["dividendPerShare"] / price) * 100, 2)
    return result


# ---------------------------------------------------------------------------
# Price history: PSX's own data portal (for the 1W/1M/5Y trend charts)
# ---------------------------------------------------------------------------
def fetch_eod_history(symbol):
    """Returns a list of (timestamp, close_price, volume) tuples, newest last,
    or None. volume is None per-row if the response doesn't include a third
    field.

    NOT VERIFIED against a live response for the volume field specifically —
    the price fields were confirmed working in a real run (2026-09-01). Run
    with PSX_DEBUG=1 and check the Action's log for "raw EOD response" if
    volume numbers look wrong or missing, and adjust the parsing below.
    """
    url = f"https://dps.psx.com.pk/timeseries/eod/{symbol}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        log(f"{symbol}: EOD history fetch failed: {e}")
        return None

    if DEBUG:
        log(f"{symbol}: raw EOD response (first 500 chars): {json.dumps(payload)[:500]}")

    rows = payload.get("data", payload) if isinstance(payload, dict) else payload
    out = []
    try:
        for row in rows:
            ts, close = row[0], row[1]
            volume = row[2] if len(row) > 2 else None
            out.append((int(ts), float(close), float(volume) if volume is not None else None))
    except Exception as e:
        log(f"{symbol}: EOD response shape unexpected ({e}) — adjust fetch_eod_history().")
        return None

    out.sort(key=lambda t: t[0])
    return out or None


def build_price_history(eod_rows):
    if not eod_rows:
        return None

    def fmt_date(ts, fmt):
        return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(fmt)

    last_week = eod_rows[-5:]
    last_month = eod_rows[-22:]

    by_year = {}
    for ts, close, _vol in eod_rows:
        year = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).year
        by_year[year] = (ts, close)
    years = sorted(by_year.keys())[-5:]
    five_year = [by_year[y] for y in years]

    return {
        "1W": [{"label": fmt_date(ts, "%a"), "price": close} for ts, close, _vol in last_week],
        "1M": [{"label": fmt_date(ts, "%d %b"), "price": close} for ts, close, _vol in last_month],
        "5Y": [{"label": fmt_date(ts, "%Y"), "price": close} for ts, close in five_year],
    }


def build_volume_stats(eod_rows):
    """Today's volume (most recent trading day) vs the 30-trading-day average.
    Returns None if the feed didn't include volume at all."""
    if not eod_rows:
        return None
    volumes = [v for _ts, _close, v in eod_rows if v is not None]
    if not volumes:
        return None
    today = volumes[-1]
    window = volumes[-22:]  # ~1 trading month, matches the 1M price chart window
    avg30 = sum(window) / len(window)
    return {"today": today, "avg30": round(avg30)}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    previous = {}
    if OUT_PATH.exists():
        try:
            previous = {s["ticker"]: s for s in json.loads(OUT_PATH.read_text())}
        except Exception as e:
            log(f"Could not read previous data.json, starting fresh: {e}")

    output = []
    for symbol, cfg in STOCK_CONFIG.items():
        log(f"Fetching {symbol}...")
        prev = previous.get(symbol, {})

        entry = {
            "ticker": symbol,
            "name": cfg["name"],
            "sector": cfg["sector"],
            "sectorPE": cfg["sectorPE"],
            "sectorPB": cfg["sectorPB"],
            "sectorROE": cfg["sectorROE"],
            "sectorDE": cfg["sectorDE"],
            "deNote": cfg["deNote"],
            "financials": STATIC_FINANCIALS[symbol],
        }

        price = fetch_price(symbol)
        if price is None:
            price = prev.get("price")
            log(f"{symbol}: keeping previous price ({price}) — live fetch failed")
        entry["price"] = price
        entry.update(compute_fundamentals(symbol, price))

        eod_rows = fetch_eod_history(symbol)
        history = build_price_history(eod_rows)
        if history:
            entry["priceHistory"] = history
        elif "priceHistory" in prev:
            log(f"{symbol}: keeping previous price history (fetch failed)")
            entry["priceHistory"] = prev["priceHistory"]
        elif "price5Y" in prev:
            entry["price5Y"] = prev["price5Y"]

        volume = build_volume_stats(eod_rows)
        if volume:
            entry["volume"] = volume
        elif "volume" in prev:
            log(f"{symbol}: keeping previous volume stats (feed had no volume this run)")
            entry["volume"] = prev["volume"]
        else:
            log(f"{symbol}: no volume data available yet")

        output.append(entry)
        time.sleep(POLITE_DELAY_SECONDS)

    OUT_PATH.write_text(json.dumps(output, indent=2))
    log(f"Wrote {OUT_PATH} ({len(output)} stocks)")


if __name__ == "__main__":
    main()
