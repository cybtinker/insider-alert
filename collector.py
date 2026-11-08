"""Insider Alert collector.

Downloads SEC EDGAR daily form indexes, then fetches and parses
Form 4 (insider transactions) and Schedule 13D/13G (5%+ ownership)
filings into a SQLite database.

Usage:
    SEC_USER_AGENT="InsiderAlert you@example.com" python collector.py --days 5
    SEC_USER_AGENT=... python collector.py --date 2026-11-06

The SEC requires a User-Agent with a contact email and allows at most
10 requests per second; this script stays under 8.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sqlite3
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("INSIDER_DB", os.path.join(HERE, "insider.db"))
ARCHIVES = "https://www.sec.gov/Archives/"
FORM4_TYPES = {"4", "4/A"}
OWNERSHIP_TYPES = {"SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A",
                   "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"}
MAX_RPS = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS filings (
    accession     TEXT PRIMARY KEY,
    form          TEXT NOT NULL,
    filed_date    TEXT NOT NULL,
    issuer_cik    TEXT,
    issuer_name   TEXT,
    ticker        TEXT,
    filer_name    TEXT,          -- 13D/G: who filed; Form 4: first reporting owner
    percent_class REAL,          -- 13D/G only, when stated
    url           TEXT NOT NULL,
    fetched_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS owners (
    accession     TEXT NOT NULL,
    owner_cik     TEXT,
    owner_name    TEXT NOT NULL,
    is_director   INTEGER,
    is_officer    INTEGER,
    is_ten_pct    INTEGER,
    officer_title TEXT,
    PRIMARY KEY (accession, owner_cik, owner_name)
);
CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    accession       TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    is_derivative   INTEGER NOT NULL,
    security        TEXT,
    trans_date      TEXT,
    code            TEXT,          -- P buy, S sell, A grant, M exercise, F tax, G gift...
    shares          REAL,
    price           REAL,
    value           REAL,
    acq_disp        TEXT,          -- A acquired / D disposed
    shares_after    REAL,
    direct_indirect TEXT,
    plan_10b5_1     INTEGER,
    UNIQUE (accession, seq, is_derivative)
);
CREATE TABLE IF NOT EXISTS index_days (
    day        TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    loaded_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_filings_date   ON filings(filed_date);
CREATE INDEX IF NOT EXISTS ix_filings_ticker ON filings(ticker);
CREATE INDEX IF NOT EXISTS ix_owners_name    ON owners(owner_name);
"""


# ---------------------------------------------------------------- HTTP

class RateLimiter:
    def __init__(self, rps: float):
        self.interval = 1.0 / rps
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if self.next_at > now:
                time.sleep(self.next_at - now)
                now = self.next_at
            self.next_at = now + self.interval


_limiter = RateLimiter(MAX_RPS)


def user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if "@" not in ua:
        sys.exit("Set SEC_USER_AGENT to a name plus contact email, e.g. "
                 "'InsiderAlert you@example.com' (required by the SEC).")
    return ua


def fetch(url: str, retries: int = 4) -> bytes | None:
    for attempt in range(retries):
        _limiter.wait()
        req = Request(url, headers={"User-Agent": user_agent(),
                                    "Accept-Encoding": "identity"})
        try:
            with urlopen(req, timeout=30) as r:
                return r.read()
        except HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 403, 500, 502, 503) and attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            raise
        except URLError:
            if attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            raise
    return None


# ---------------------------------------------------------------- index

IDX_LINE = re.compile(r"^(\S.*?)\s{2,}(.*?)\s{2,}(\d+)\s+(\d{8})\s+(edgar/\S+)\s*$")


def quarter(d: dt.date) -> int:
    return (d.month - 1) // 3 + 1


def load_day_index(day: dt.date) -> list[dict] | None:
    url = (f"{ARCHIVES}edgar/daily-index/{day.year}/QTR{quarter(day)}/"
           f"form.{day:%Y%m%d}.idx")
    raw = fetch(url)
    if raw is None:
        return None  # weekend / holiday
    rows, seen = [], set()
    for line in raw.decode("latin-1").splitlines():
        m = IDX_LINE.match(line)
        if not m:
            continue
        form, company, cik, filed, path = (s.strip() for s in m.groups())
        if form not in FORM4_TYPES and form not in OWNERSHIP_TYPES:
            continue
        if path in seen:  # each filing is listed once per party
            continue
        seen.add(path)
        rows.append({"form": form, "company": company, "cik": cik,
                     "filed": f"{filed[:4]}-{filed[4:6]}-{filed[6:]}",
                     "path": path})
    return rows


def accession_of(path: str) -> str:
    return os.path.basename(path).removesuffix(".txt")


def index_url(path: str) -> str:
    # edgar/data/CIK/0001234567-26-000123.txt -> folder index page
    cik = path.split("/")[2]
    acc = accession_of(path)
    return f"{ARCHIVES}edgar/data/{cik}/{acc.replace('-', '')}/{acc}-index.htm"


# ---------------------------------------------------------------- parsing

def _t(el, path: str) -> str | None:
    if el is None:
        return None
    node = el.find(path)
    if node is None:
        return None
    v = node.find("value")
    txt = (v.text if v is not None else node.text) or ""
    txt = txt.strip()
    return txt or None


def _f(el, path: str) -> float | None:
    v = _t(el, path)
    if v is None:
        return None
    try:
        return float(v.replace(",", ""))
    except ValueError:
        return None


def _b(el, path: str) -> int:
    v = (_t(el, path) or "").lower()
    return 1 if v in ("1", "true") else 0


def parse_form4(text: str) -> dict | None:
    m = re.search(r"<ownershipDocument>.*?</ownershipDocument>", text, re.S)
    if not m:
        return None
    root = ET.fromstring(m.group(0))
    issuer = root.find("issuer")
    doc_10b5 = _b(root, "aff10b5One")
    owners = []
    for ro in root.findall("reportingOwner"):
        rel = ro.find("reportingOwnerRelationship")
        owners.append({
            "cik": _t(ro, "reportingOwnerId/rptOwnerCik"),
            "name": _t(ro, "reportingOwnerId/rptOwnerName") or "Unknown",
            "is_director": _b(rel, "isDirector"),
            "is_officer": _b(rel, "isOfficer"),
            "is_ten_pct": _b(rel, "isTenPercentOwner"),
            "title": _t(rel, "officerTitle") or (_t(rel, "otherText")),
        })
    txs = []
    for is_deriv, table, tag in ((0, "nonDerivativeTable", "nonDerivativeTransaction"),
                                 (1, "derivativeTable", "derivativeTransaction")):
        tbl = root.find(table)
        if tbl is None:
            continue
        for i, tx in enumerate(tbl.findall(tag)):
            shares = _f(tx, "transactionAmounts/transactionShares")
            price = _f(tx, "transactionAmounts/transactionPricePerShare")
            txs.append({
                "seq": i, "is_derivative": is_deriv,
                "security": _t(tx, "securityTitle"),
                "date": (_t(tx, "transactionDate") or "")[:10] or None,
                "code": _t(tx, "transactionCoding/transactionCode"),
                "shares": shares, "price": price,
                "value": round(shares * price, 2) if shares and price else None,
                "acq_disp": _t(tx, "transactionAmounts/transactionAcquiredDisposedCode"),
                "after": _f(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction"),
                "di": _t(tx, "ownershipNature/directOrIndirectOwnership"),
                "plan": doc_10b5,
            })
    return {
        "issuer_cik": _t(issuer, "issuerCik"),
        "issuer_name": _t(issuer, "issuerName"),
        "ticker": (_t(issuer, "issuerTradingSymbol") or "").upper() or None,
        "owners": owners, "txs": txs,
    }


def _header_block(text: str, label: str) -> str:
    m = re.search(label + r":\s*\n(.*?)(?:\n\S|\Z)", text, re.S)
    return m.group(1) if m else ""


def parse_13dg(text: str) -> dict:
    head = text.split("</SEC-HEADER>")[0]
    subj = _header_block(head, "SUBJECT COMPANY")
    filer = _header_block(head, "FILED BY")
    name = lambda b: (re.search(r"COMPANY CONFORMED NAME:\s*(.+)", b) or [None, None])[1]
    cik = lambda b: (re.search(r"CENTRAL INDEX KEY:\s*(\d+)", b) or [None, None])[1]
    pct = None
    m = re.search(r"<(?:\w+:)?percentOfClass>\s*([\d.]+)", text)
    if m:
        try:
            pct = float(m.group(1))
        except ValueError:
            pass
    return {"issuer_cik": (cik(subj) or "").lstrip("0") or None,
            "issuer_name": (name(subj) or "").strip() or None,
            "filer": (name(filer) or "").strip() or None,
            "pct": pct}


# ---------------------------------------------------------------- tickers

def load_ticker_map(con: sqlite3.Connection) -> dict[str, str]:
    con.execute("CREATE TABLE IF NOT EXISTS tickers (cik TEXT PRIMARY KEY, ticker TEXT, name TEXT)")
    if con.execute("SELECT COUNT(*) FROM tickers").fetchone()[0] == 0:
        import json
        raw = fetch("https://www.sec.gov/files/company_tickers.json")
        if raw:
            data = json.loads(raw)
            con.executemany("INSERT OR REPLACE INTO tickers VALUES (?,?,?)",
                            [(str(v["cik_str"]), v["ticker"], v["title"]) for v in data.values()])
            con.commit()
    return dict(con.execute("SELECT cik, ticker FROM tickers"))


# ---------------------------------------------------------------- load

def process(row: dict) -> tuple[dict, dict | None]:
    raw = fetch(ARCHIVES + row["path"])
    if raw is None:
        return row, None
    text = raw.decode("utf-8", "replace")
    try:
        if row["form"] in FORM4_TYPES:
            return row, {"kind": "4", **(parse_form4(text) or {})}
        return row, {"kind": "13", **parse_13dg(text)}
    except ET.ParseError:
        return row, None


def store(con, row, parsed, tickers):
    acc = accession_of(row["path"])
    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    if parsed["kind"] == "4":
        owners = parsed.get("owners", [])
        con.execute("INSERT OR REPLACE INTO filings VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (acc, row["form"], row["filed"], parsed.get("issuer_cik"),
                     parsed.get("issuer_name"),
                     parsed.get("ticker") or tickers.get((parsed.get("issuer_cik") or "").lstrip("0")),
                     owners[0]["name"] if owners else None, None,
                     index_url(row["path"]), now))
        for o in owners:
            con.execute("INSERT OR REPLACE INTO owners VALUES (?,?,?,?,?,?,?)",
                        (acc, o["cik"], o["name"], o["is_director"], o["is_officer"],
                         o["is_ten_pct"], o["title"]))
        for t in parsed.get("txs", []):
            con.execute("""INSERT OR REPLACE INTO transactions
                (accession, seq, is_derivative, security, trans_date, code, shares, price,
                 value, acq_disp, shares_after, direct_indirect, plan_10b5_1)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (acc, t["seq"], t["is_derivative"], t["security"], t["date"],
                         t["code"], t["shares"], t["price"], t["value"], t["acq_disp"],
                         t["after"], t["di"], t["plan"]))
    else:
        icik = parsed.get("issuer_cik")
        con.execute("INSERT OR REPLACE INTO filings VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (acc, row["form"], row["filed"], icik, parsed.get("issuer_name"),
                     tickers.get(icik or ""), parsed.get("filer"), parsed.get("pct"),
                     index_url(row["path"]), now))


def collect_day(con, day: dt.date, tickers, force=False) -> int:
    if not force and con.execute("SELECT 1 FROM index_days WHERE day=? AND status='done'",
                                 (day.isoformat(),)).fetchone():
        print(f"{day} already loaded")
        return 0
    rows = load_day_index(day)
    if rows is None:
        print(f"{day}: no index (weekend/holiday)")
        return 0
    have = {r[0] for r in con.execute("SELECT accession FROM filings WHERE filed_date=?",
                                       (day.isoformat(),))}
    todo = [r for r in rows if accession_of(r["path"]) not in have]
    print(f"{day}: {len(rows)} filings, {len(todo)} to fetch", flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(process, r) for r in todo]
        for f in as_completed(futs):
            try:
                row, parsed = f.result()
            except Exception as e:  # keep going on single bad filings
                print("  error:", e, file=sys.stderr)
                continue
            if parsed:
                store(con, row, parsed, tickers)
                done += 1
                if done % 200 == 0:
                    con.commit()
                    print(f"  {done}/{len(todo)}", flush=True)
    con.execute("INSERT OR REPLACE INTO index_days VALUES (?,?,?)",
                (day.isoformat(), "done", dt.datetime.utcnow().isoformat(timespec="seconds")))
    con.commit()
    return done


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    return con


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD; default: last N business days")
    ap.add_argument("--days", type=int, default=1, help="how many calendar days back")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    user_agent()
    con = connect()
    tickers = load_ticker_map(con)
    if a.date:
        days = [dt.date.fromisoformat(a.date)]
    else:
        today = dt.date.today()
        days = [today - dt.timedelta(days=i) for i in range(a.days, -1, -1)]
    for d in days:
        if d.weekday() < 5:
            collect_day(con, d, tickers, a.force)


if __name__ == "__main__":
    main()
