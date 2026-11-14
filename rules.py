"""Regulatory event study: how sector funds moved after significant federal rules.

Collects two things into insider.db and measures one:

  1. Significant proposed and final rules (Executive Order 12866) from the
     Federal Register API, with the agencies that issued them. Free, no key.
  2. Daily prices for SPY and the 11 SPDR sector funds (XLK, XLF, ...).
  3. For each rule, the return of the sector funds its agency regulates
     minus SPY's return ("excess return") from two trading days before
     publication (rules are posted for public inspection a day early) to
     1, 5 and 20 trading days after. Averages are compared with what the
     same fund does over any window of the same length, so a fund that
     simply rose all year doesn't look like it reacted to every rule.

report.py calls study() and writes site/rules.json for the Rules tab.

Usage: python rules.py [--years 5]
This is a historical average, not a forecast, and not investment advice.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import io
import json
import math
import os
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("INSIDER_DB", os.path.join(HERE, "insider.db"))
UA = "Mozilla/5.0 (compatible; InsiderAlert/1.0; +https://github.com/cybtinker/insider-alert)"

SECTORS = {
    "XLK": "Technology", "XLC": "Communications", "XLY": "Consumer discretionary",
    "XLP": "Consumer staples", "XLV": "Health care", "XLF": "Financials",
    "XLE": "Energy", "XLI": "Industrials", "XLB": "Materials",
    "XLU": "Utilities", "XLRE": "Real estate",
}
FUNDS = ["SPY", *SECTORS]

# Agency name (lower case, matched as a substring) -> sector funds it mostly regulates.
# A rule from several agencies counts for every sector any of them maps to.
AGENCY_SECTORS = [
    ("federal reserve system", ["XLF"]),
    ("comptroller of the currency", ["XLF"]),
    ("federal deposit insurance", ["XLF"]),
    ("consumer financial protection", ["XLF"]),
    ("securities and exchange commission", ["XLF"]),
    ("commodity futures trading", ["XLF"]),
    ("national credit union", ["XLF"]),
    ("financial crimes enforcement", ["XLF"]),
    ("federal housing finance", ["XLF", "XLRE"]),
    ("housing and urban development", ["XLRE"]),
    ("food and drug administration", ["XLV"]),
    ("centers for medicare", ["XLV"]),
    ("health and human services", ["XLV"]),
    ("health resources and services", ["XLV"]),
    ("federal energy regulatory", ["XLU", "XLE"]),
    ("nuclear regulatory", ["XLU"]),
    ("energy department", ["XLE", "XLU"]),
    ("energy efficiency and renewable energy", ["XLU", "XLY"]),
    ("land management bureau", ["XLE"]),
    ("ocean energy management", ["XLE"]),
    ("safety and environmental enforcement", ["XLE"]),
    ("pipeline and hazardous materials", ["XLE"]),
    ("environmental protection agency", ["XLE", "XLU", "XLB"]),
    ("federal communications commission", ["XLC"]),
    ("industry and security bureau", ["XLK"]),
    ("federal trade commission", ["XLY"]),
    ("national highway traffic safety", ["XLY"]),
    ("federal aviation administration", ["XLI"]),
    ("federal railroad", ["XLI"]),
    ("federal motor carrier", ["XLI"]),
    ("surface transportation board", ["XLI"]),
    ("federal maritime commission", ["XLI"]),
    ("occupational safety and health", ["XLI"]),
    ("defense acquisition regulations", ["XLI"]),
    ("mine safety and health", ["XLB"]),
    ("agriculture department", ["XLP"]),
    ("agricultural marketing service", ["XLP"]),
    ("food safety and inspection", ["XLP"]),
    ("alcohol and tobacco tax", ["XLP"]),
]
HORIZONS = (1, 5, 20)
PATH = range(-1, 21)          # trading days relative to publication, for the average path

SCHEMA = """
CREATE TABLE IF NOT EXISTS fr_rules (
    doc       TEXT PRIMARY KEY,   -- Federal Register document number
    pub       TEXT NOT NULL,      -- publication date YYYY-MM-DD
    kind      TEXT NOT NULL,      -- Final / Proposed
    title     TEXT,
    agencies  TEXT,               -- JSON list of agency names
    url       TEXT
);
CREATE INDEX IF NOT EXISTS fr_rules_pub ON fr_rules(pub);
CREATE TABLE IF NOT EXISTS etf_prices (
    ticker    TEXT NOT NULL,
    day       TEXT NOT NULL,
    close     REAL NOT NULL,      -- adjusted for dividends and splits when the source gives it
    PRIMARY KEY (ticker, day)
);
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    return con


def get(url: str, retries: int = 4, accept: str = "*/*") -> bytes | None:
    for attempt in range(retries):
        try:
            with urlopen(Request(url, headers={"User-Agent": UA, "Accept": accept}), timeout=60) as r:
                return r.read()
        except HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (403, 429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(3 * 2 ** attempt)
                continue
            print(f"  {url[:90]}: HTTP {e.code}")
            return None
        except (URLError, ConnectionError, TimeoutError) as e:
            if attempt < retries - 1:
                time.sleep(3 * 2 ** attempt)
                continue
            print(f"  {url[:90]}: {e}")
            return None
    return None


# ---------------------------------------------------------------- rules

def sectors_for(agencies: list[str]) -> list[str]:
    out = []
    for a in agencies:
        low = a.lower()
        for key, secs in AGENCY_SECTORS:
            if key in low:
                out += [s for s in secs if s not in out]
    return out


def collect_rules(con, start: dt.date, end: dt.date) -> int:
    """Significant rules published between start and end, fetched a quarter at a time."""
    n = 0
    lo = start
    while lo <= end:
        hi = min(end, lo + dt.timedelta(days=91))
        params = [("per_page", "1000"), ("order", "oldest"),
                  ("conditions[type][]", "RULE"), ("conditions[type][]", "PRORULE"),
                  ("conditions[significant]", "1"),
                  ("conditions[publication_date][gte]", lo.isoformat()),
                  ("conditions[publication_date][lte]", hi.isoformat())]
        params += [("fields[]", f) for f in
                   ("document_number", "publication_date", "type", "title", "agencies", "html_url", "significant")]
        url = "https://www.federalregister.gov/api/v1/documents.json?" + urlencode(params)
        while url:
            raw = get(url, accept="application/json")
            if not raw:
                break
            page = json.loads(raw)
            for d in page.get("results") or []:
                if d.get("significant") is False:
                    continue
                kind = {"Rule": "Final", "Proposed Rule": "Proposed"}.get(d.get("type"))
                if not kind:
                    continue
                names = [a.get("name") or a.get("raw_name") or "" for a in d.get("agencies") or []]
                con.execute("INSERT OR REPLACE INTO fr_rules VALUES (?,?,?,?,?,?)",
                            (d["document_number"], d["publication_date"], kind, (d.get("title") or "").strip(),
                             json.dumps([x for x in names if x]), d.get("html_url")))
                n += 1
            url = page.get("next_page_url")
        con.commit()
        lo = hi + dt.timedelta(days=1)
    return n


# ---------------------------------------------------------------- prices

def _yahoo(ticker: str, start: dt.date) -> list[tuple[str, float]]:
    p1 = int(dt.datetime.combine(start, dt.time()).replace(tzinfo=dt.timezone.utc).timestamp())
    p2 = int(time.time()) + 86400
    for host in ("query1", "query2"):
        raw = get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}?period1={p1}&period2={p2}"
                  f"&interval=1d&events=div%2Csplit", retries=3)
        if not raw:
            continue
        try:
            res = json.loads(raw)["chart"]["result"][0]
            off = res["meta"].get("gmtoffset", -18000)
            closes = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") \
                or res["indicators"]["quote"][0]["close"]
            out = []
            for ts, c in zip(res["timestamp"], closes):
                if c:
                    out.append((dt.datetime.fromtimestamp(ts + off, dt.timezone.utc).date().isoformat(), float(c)))
            if out:
                return out
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return []


def _stooq(ticker: str, start: dt.date) -> list[tuple[str, float]]:
    raw = get(f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d&d1={start:%Y%m%d}", retries=2)
    if not raw or not raw.startswith(b"Date"):
        return []
    return [(r["Date"], float(r["Close"])) for r in csv.DictReader(io.StringIO(raw.decode()))
            if r.get("Close") not in (None, "", "null")]


def collect_prices(con, start: dt.date) -> dict:
    got = {}
    for t in FUNDS:
        rows = _yahoo(t, start) or _stooq(t, start)
        if rows:
            con.execute("DELETE FROM etf_prices WHERE ticker = ?", (t,))
            con.executemany("INSERT OR REPLACE INTO etf_prices VALUES (?,?,?)", [(t, d, c) for d, c in rows])
            con.commit()
        got[t] = len(rows)
        time.sleep(1)
    return got


# ---------------------------------------------------------------- study

def _mean_sd(xs):
    n = len(xs)
    if n == 0:
        return None, None
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else None
    return m, sd


def _stat(xs, base):
    m, sd = _mean_sd(xs)
    if m is None:
        return None
    t = (m - base) / (sd / math.sqrt(len(xs))) if sd else None
    return {"n": len(xs), "avg": round(m - base, 5), "t": round(t, 2) if t is not None else None,
            "down": round(sum(1 for x in xs if x < 0) / len(xs), 3)}


def study(con) -> dict | None:
    """Average excess returns of sector funds after significant rules from the agencies that regulate them."""
    try:
        px = {}
        for t, d, c in con.execute("SELECT ticker, day, close FROM etf_prices ORDER BY day"):
            px.setdefault(t, {})[d] = c
        rules = con.execute("SELECT doc, pub, kind, title, agencies, url FROM fr_rules ORDER BY pub").fetchall()
    except sqlite3.OperationalError:
        return None
    if "SPY" not in px or not rules:
        return None
    days = sorted(px["SPY"])
    spy = [px["SPY"][d] for d in days]
    series = {t: [px.get(t, {}).get(d) for d in days] for t in SECTORS}

    def excess(t, i0, i1):
        s = series[t]
        if i0 < 0 or i1 >= len(days) or not s[i0] or not s[i1]:
            return None
        return s[i1] / s[i0] - spy[i1] / spy[i0]

    # What each fund does versus SPY over any window of the same length: the baseline to beat.
    base = {}
    for t in SECTORS:
        for k in PATH:
            w = k + 2
            xs = [x for i in range(len(days) - w) if (x := excess(t, i, i + w)) is not None]
            base[(t, k)] = sum(xs) / len(xs) if xs else 0.0

    events = {}      # (sector, kind, start index) -> list of rules that day
    recent, cutoff = [], (dt.date.fromisoformat(days[-1]) - dt.timedelta(days=120)).isoformat()
    for doc, pub, kind, title, agencies, url in rules:
        ags = json.loads(agencies or "[]")
        secs = sectors_for(ags)
        i0 = bisect.bisect_left(days, pub)        # first trading day on or after publication
        for t in secs:
            events.setdefault((t, kind, i0), []).append(doc)
        if pub >= cutoff and secs:
            moves = {}
            for t in secs:
                moves[t] = [round(x, 5) if (x := excess(t, i0 - 2, i0 + h - 1)) is not None else None for h in HORIZONS]
            recent.append({"doc": doc, "date": pub, "kind": kind, "title": title, "agencies": ags,
                           "url": url, "moves": moves})

    groups, paths, biggest = {}, {}, []
    for (t, kind, i0), docs in events.items():
        cars = [excess(t, i0 - 2, i0 + h - 1) for h in HORIZONS]
        for key in ((t, kind), (t, "All")):
            g = groups.setdefault(key, [[] for _ in HORIZONS])
            for j, x in enumerate(cars):
                if x is not None:
                    g[j].append(x)
            p = paths.setdefault(key, {k: [] for k in PATH})
            for k in PATH:
                x = excess(t, i0 - 2, i0 + k)
                if x is not None:
                    p[k].append(x - base[(t, k)])
        if cars[-1] is not None:
            biggest.append((abs(cars[-1]), t, kind, days[min(i0, len(days) - 1)], docs, cars))

    table = []
    for (t, kind), g in sorted(groups.items()):
        row = {"sector": t, "name": SECTORS[t], "kind": kind,
               "h": [_stat(g[j], base[(t, h - 1)]) for j, h in enumerate(HORIZONS)]}
        pk = paths[(t, kind)]
        row["path"] = [round(sum(pk[k]) / len(pk[k]), 5) if pk[k] else None for k in PATH]
        table.append(row)

    # By agency: the same numbers grouped by who issued the rule, for agencies with enough rules.
    by_agency = {}
    for doc, pub, kind, title, agencies, url in rules:
        i0 = bisect.bisect_left(days, pub)
        for a in json.loads(agencies or "[]"):
            for t in sectors_for([a]):
                x5, x20 = excess(t, i0 - 2, i0 + 4), excess(t, i0 - 2, i0 + 19)
                e = by_agency.setdefault((a, t), [[], []])
                if x5 is not None:
                    e[0].append(x5)
                if x20 is not None:
                    e[1].append(x20)
    agencies_out = sorted(
        ({"agency": a, "sector": t, "h5": _stat(v[0], base[(t, 4)]), "h20": _stat(v[1], base[(t, 19)])}
         for (a, t), v in by_agency.items() if len(v[0]) >= 8),
        key=lambda r: -r["h5"]["n"])

    docs_by_id = {r[0]: r for r in rules}
    biggest.sort(reverse=True)
    big_out = []
    for _, t, kind, day, docs, cars in biggest[:40]:
        r = docs_by_id[docs[0]]
        big_out.append({"sector": t, "kind": kind, "date": r[1], "title": r[3], "url": r[5],
                        "agencies": json.loads(r[4] or "[]"), "more": len(docs) - 1,
                        "moves": [round(x, 5) if x is not None else None for x in cars]})

    return {"sectors": SECTORS, "horizons": list(HORIZONS), "path_days": list(PATH),
            "from": rules[0][1], "to": rules[-1][1], "prices_to": days[-1],
            "rules": len(rules), "events": len(events),
            "table": table, "agencies": agencies_out[:40], "biggest": big_out,
            "recent": sorted(recent, key=lambda r: r["date"], reverse=True)[:300]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=5, help="history to backfill on the first run")
    a = ap.parse_args()
    con = connect()
    today = dt.date.today()
    start = today - dt.timedelta(days=365 * a.years + 40)
    last = con.execute("SELECT MAX(pub) FROM fr_rules").fetchone()[0]
    first = con.execute("SELECT MIN(pub) FROM fr_rules").fetchone()[0]
    if last and first and first <= (start + dt.timedelta(days=60)).isoformat():
        rstart = dt.date.fromisoformat(last) - dt.timedelta(days=30)   # refresh the last month
    else:
        rstart = start
    n = collect_rules(con, rstart, today)
    print(f"Federal Register: {n} significant rules fetched from {rstart}")
    got = collect_prices(con, start)
    print("Prices: " + ", ".join(f"{t} {c}" for t, c in got.items()))
    missing = [t for t, c in got.items() if not c]
    if missing:
        print(f"  no prices for {', '.join(missing)} this run (kept any stored ones)")
    s = study(con)
    if s:
        print(f"Study: {s['rules']} rules, {s['events']} sector-days, {s['from']} to {s['to']}")


if __name__ == "__main__":
    main()
