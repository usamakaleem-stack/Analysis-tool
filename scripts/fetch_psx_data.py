#!/usr/bin/env python3
"""
Fetches PSX price + fundamentals data and writes data.json for the
PSX Fair Value Ledger dashboard (psx-fair-value-dashboard.html).

Place this file at:  scripts/fetch_psx_data.py   (in your repo)
Run it with:          python scripts/fetch_psx_data.py
It writes:            data.json   (next to index.html, at the repo root)

DATA SOURCES
------------
1. scstrade.com company snapshot page
   https://www.scstrade.com/stockscreening/SS_CompanySnapShot.aspx?symbol=<TICKER>
   -> current price, P/E, EPS, Book Value/share, ROE, D/E, dividend yield.
   CONFIRMED: this page's HTML was fetched and inspected directly (2026-08-31).
   It renders each metrics section as a table where one row holds the labels
   ("Price To Earning P/E Upto 2026 4Q", "Return On Equity Upto 2026 4Q", ...)
   and the next row holds the matching values, in the same order. This script
   flattens every such table into a label->value dict and looks values up by
   a case-insensitive substring match, so it survives the quarter/year in the
   label text changing every quarter.

2. PSX's own data portal, dps.psx.com.pk
   https://dps.psx.com.pk/timeseries/eod/<TICKER>  -> daily end-of-day price history
   Several independent open-source PSX tools use this endpoint and describe
   it as returning JSON. Its exact response shape was NOT verified against a
   live response while writing this script (this environment could not reach
   dps.psx.com.pk to check) — see fetch_price_history() below. Run this once
   with PSX_DEBUG=1 and inspect the printed raw response for one symbol
   before trusting the parsed output; adjust the parsing there if the shape
   differs from what's assumed.

WHAT THIS SCRIPT DOES NOT DO
-----------------------------
Five-year revenue / operating-expense history is NOT scraped live. The sites
that show multi-year income statements render that table with JavaScript, not
in the plain HTML a script like this can read. STATIC_FINANCIALS below holds
hand-entered figures — refresh those by hand once or twice a year when annual
reports come out, or replace this block once you have a better source
(e.g. a paid financial-data API, or parsing the PDF annual reports directly).

ROBUSTNESS
----------
Every network call is wrapped so one symbol's failure (site down, HTML
changed, rate limited) does not crash the whole run or blank out that
stock's numbers — it logs the problem and falls back to the previous
data.json value for that field.
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
# Static config: sector benchmarks and things that aren't reliably scrapable
# yet. Add a new stock by adding one entry here (and one in STATIC_FINANCIALS).
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

# Manually curated 5-yr revenue/opex (PKR bn). See docstring above.
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
# Fundamentals: scstrade.com company snapshot page
# ---------------------------------------------------------------------------
def parse_label_value_tables(soup):
    """scstrade's snapshot page renders each metrics section as a table where
    one <tr> holds the labels and the next <tr> holds the matching values, in
    the same order (confirmed against a live fetch of the LUCK page on
    2026-08-31). Flatten every such table on the page into one dict."""
    data = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for i in range(len(rows) - 1):
            label_cells = rows[i].find_all(["th", "td"])
            value_cells = rows[i + 1].find_all(["th", "td"])
            if len(label_cells) == len(value_cells) and len(label_cells) > 1:
                for lc, vc in zip(label_cells, value_cells):
                    label = lc.get_text(strip=True)
                    value = vc.get_text(strip=True)
                    if label and value:
                        data[label] = value
    return data


def find_value(data, *substrings):
    """Case-insensitive substring match across all given fragments, e.g.
    find_value(data, "price to earning") matches "Price To Earning P/E Upto 2026 4Q"."""
    for label, value in data.items():
        low = label.lower()
        if all(s.lower() in low for s in substrings):
            return value
    return None


def parse_number(raw):
    if raw is None:
        return None
    cleaned = raw.replace("Rs.", "").replace("Rs", "").replace(",", "") \
                 .replace("x", "").replace("%", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def fetch_fundamentals(symbol):
    """Returns a dict of scraped fundamentals, or None if the fetch/parse fails."""
    url = f"https://www.scstrade.com/stockscreening/SS_CompanySnapShot.aspx?symbol={symbol.lower()}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        log(f"{symbol}: fundamentals fetch failed: {e}")
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    data = parse_label_value_tables(soup)
    if DEBUG:
        log(f"{symbol}: parsed {len(data)} label/value pairs")

    eps  = parse_number(find_value(data, "last annual eps"))
    pe   = parse_number(find_value(data, "price to earning"))
    bvps = parse_number(find_value(data, "book value", "upto"))
    pb   = parse_number(find_value(data, "price to book value"))
    roe  = parse_number(find_value(data, "return on equity"))
    de   = parse_number(find_value(data, "total debt to equity"))
    divy = parse_number(find_value(data, "dividend yield"))

    # The live price sits near the top of the page outside these label/value
    # tables (e.g. "Rs. 437.33"), so pull the first "Rs. <number>" on the page.
    price = None
    m = re.search(r"Rs\.\s*([\d,]+\.\d+)", soup.get_text())
    if m:
        price = float(m.group(1).replace(",", ""))

    result = {}
    if price is not None: result["price"] = price
    if eps is not None:   result["eps"] = eps
    if pe is not None:    result["pe"] = pe
    if bvps is not None:  result["bvps"] = bvps
    if pb is not None:    result["pb"] = pb
    if roe is not None:   result["roe"] = roe
    if de is not None:    result["de"] = round(de / 100, 4)  # page gives a %, schema uses a ratio
    if divy is not None:  result["divYield"] = divy

    missing = [k for k in ("price", "eps", "pe", "bvps", "pb", "roe", "divYield") if k not in result]
    if missing:
        log(f"{symbol}: could not find {missing} on the page — label text may have "
            f"changed. Run with PSX_DEBUG=1 and inspect the labels.")
    return result or None


# ---------------------------------------------------------------------------
# Price history: PSX's own data portal
# ---------------------------------------------------------------------------
def fetch_eod_history(symbol):
    """Returns a list of (timestamp, close_price) tuples, newest last, or None.

    NOT VERIFIED against a live response — see the module docstring. Several
    independent scrapers use this URL and describe it as JSON; the exact key
    names below are a best guess and may need adjusting. Run with
    PSX_DEBUG=1 to print the raw response for the first symbol and fix the
    parsing here if it doesn't match.
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
            # common shape seen across similar PSX scrapers: [timestamp, close, volume]
            ts, close = row[0], row[1]
            out.append((int(ts), float(close)))
    except Exception as e:
        log(f"{symbol}: EOD response shape unexpected ({e}) — adjust fetch_eod_history().")
        return None

    out.sort(key=lambda t: t[0])
    return out or None


def build_price_history(eod_rows):
    """Turns raw (timestamp, close) rows into the 1W / 1M / 5Y series the
    dashboard expects: [{"label": ..., "price": ...}, ...] each."""
    if not eod_rows:
        return None

    def fmt_date(ts, fmt):
        return datetime.datetime.utcfromtimestamp(ts).strftime(fmt)

    last_week = eod_rows[-5:]
    last_month = eod_rows[-22:]

    # one point per year for the last 5 years: closest trading day to Dec 31
    by_year = {}
    for ts, close in eod_rows:
        year = datetime.datetime.utcfromtimestamp(ts).year
        by_year[year] = (ts, close)  # keeps overwriting -> ends up as latest entry seen per year
    years = sorted(by_year.keys())[-5:]
    five_year = [by_year[y] for y in years]

    return {
        "1W": [{"label": fmt_date(ts, "%a"), "price": close} for ts, close in last_week],
        "1M": [{"label": fmt_date(ts, "%d %b"), "price": close} for ts, close in last_month],
        "5Y": [{"label": fmt_date(ts, "%Y"), "price": close} for ts, close in five_year],
    }


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

        fundamentals = fetch_fundamentals(symbol)
        if fundamentals:
            entry.update(fundamentals)
        else:
            log(f"{symbol}: keeping previous fundamentals (fetch failed)")
            for k in ("price", "eps", "pe", "bvps", "pb", "roe", "divYield", "de"):
                if k in prev:
                    entry[k] = prev[k]

        # banks: D/E is never meaningful, regardless of what got scraped
        if cfg["sectorDE"] is None:
            entry["de"] = None

        eod_rows = fetch_eod_history(symbol)
        history = build_price_history(eod_rows)
        if history:
            entry["priceHistory"] = history
        elif "priceHistory" in prev:
            log(f"{symbol}: keeping previous price history (fetch failed)")
            entry["priceHistory"] = prev["priceHistory"]
        elif "price5Y" in prev:
            entry["price5Y"] = prev["price5Y"]

        output.append(entry)
        time.sleep(POLITE_DELAY_SECONDS)

    OUT_PATH.write_text(json.dumps(output, indent=2))
    log(f"Wrote {OUT_PATH} ({len(output)} stocks)")


if __name__ == "__main__":
    main()
