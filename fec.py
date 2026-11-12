"""Campaign donations from company insiders to members of Congress.

For each insider who made an open-market trade in a stock that a member of
Congress also traded, look up their individual contributions in the Federal
Election Commission's Schedule A data (api.open.fec.gov) and keep the ones
that went to a sitting member's campaign committee.

A contribution only counts when the donor's last name, first name and
employer all match the insider and the company, because FEC search is by
name and common names would otherwise pull in strangers.

Needs a free key from https://api.data.gov/signup/ in FEC_API_KEY (the shared
DEMO_KEY allows too few requests to be useful). Each insider is re-checked
every 30 days.

Usage: FEC_API_KEY=... python fec.py [--budget 800]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from collector import connect
from committees import _yaml

API = "https://api.open.fec.gov/v1/"
RECHECK_DAYS = 30
CYCLES = (2024, 2026)

SCHEMA = """
CREATE TABLE IF NOT EXISTS legislator_fec (
    fec_id    TEXT PRIMARY KEY,   -- FEC candidate id, e.g. H8CA05035
    bioguide  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fec_checked (
    owner_key TEXT PRIMARY KEY,   -- owner CIK (or name), as report.py's pkey
    name      TEXT,
    issuer    TEXT,
    checked   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fec_donations (
    sub_id      TEXT PRIMARY KEY, -- FEC's id for the line item
    owner_key   TEXT NOT NULL,
    bioguide    TEXT NOT NULL,
    candidate   TEXT,
    committee   TEXT,
    date        TEXT,
    amount      REAL,
    donor       TEXT,
    employer    TEXT,
    occupation  TEXT,
    url         TEXT
);
CREATE INDEX IF NOT EXISTS ix_fec_owner ON fec_donations(owner_key);
"""

_SUFFIX = {"INC", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LLC", "PLC", "LP", "THE", "HOLDINGS",
           "HOLDING", "GROUP", "INCORPORATED", "SA", "NV", "AG", "SE", "TRUST", "BANCORP", "AND", "OF"}


def company_word(issuer: str) -> str | None:
    """The distinctive first word of a company name: 'Apple Inc.' -> 'APPLE'."""
    for w in re.sub(r"[^A-Z0-9 ]", " ", (issuer or "").upper()).split():
        if w not in _SUFFIX and len(w) >= 3:
            return w
    return None


def split_name(owner_name: str) -> tuple[str, str] | None:
    """SEC lists people surname first: 'COOK TIMOTHY D' -> ('COOK', 'TIMOTHY')."""
    parts = re.sub(r"[^A-Z \-']", " ", owner_name.upper()).split()
    parts = [p for p in parts if p not in ("JR", "SR", "II", "III", "IV", "MD", "PHD", "DR")]
    if len(parts) < 2 or len(parts[1]) < 2:
        return None
    return parts[0], parts[1]


class RateLimited(Exception):
    pass


def api(path: str, key: str, params: list[tuple[str, str]]) -> dict | None:
    url = API + path + "?" + urlencode(params + [("api_key", key)])
    for attempt in range(4):
        time.sleep(0.4)
        try:
            with urlopen(Request(url, headers={"User-Agent": "InsiderAlert"}), timeout=60) as r:
                return json.loads(r.read())
        except HTTPError as e:
            if e.code == 429:
                raise RateLimited()
            if e.code in (500, 502, 503, 504) and attempt < 3:
                time.sleep(10 * (attempt + 1))
                continue
            raise
        except (URLError, ConnectionError, TimeoutError):
            if attempt < 3:
                time.sleep(10 * (attempt + 1))
                continue
            raise
    return None


def load_fec_ids(con) -> dict[str, str]:
    con.execute("DELETE FROM legislator_fec")
    for p in _yaml("legislators-current.yaml"):
        for f in p["id"].get("fec") or []:
            con.execute("INSERT OR REPLACE INTO legislator_fec VALUES (?,?)", (f, p["id"]["bioguide"]))
    con.commit()
    return dict(con.execute("SELECT fec_id, bioguide FROM legislator_fec"))


def insiders_to_check(con) -> list[tuple[str, str, str]]:
    """Insiders with an open-market trade in the last ~3 months in a stock a
    member of Congress traded, not checked in the last RECHECK_DAYS."""
    since = (dt.date.today() - dt.timedelta(days=95)).isoformat()
    stale = (dt.date.today() - dt.timedelta(days=RECHECK_DAYS)).isoformat()
    try:
        rows = con.execute("""
            SELECT COALESCE(o.owner_cik, o.owner_name) AS k, o.owner_name, f.issuer_name, SUM(t.value) AS v
            FROM filings f JOIN transactions t USING (accession) JOIN owners o USING (accession)
            WHERE f.filed_date >= ? AND f.form IN ('4','4/A') AND t.code IN ('P','S')
              AND t.is_derivative = 0 AND t.value >= 10000
              AND f.ticker IN (SELECT DISTINCT ticker FROM congress_trades WHERE ticker != '')
              AND COALESCE(o.owner_cik, o.owner_name) NOT IN (SELECT owner_key FROM fec_checked WHERE checked >= ?)
            GROUP BY k ORDER BY v DESC""", (since, stale)).fetchall()
    except Exception as e:
        print("No trades to check yet:", e)
        return []
    return [(k, n, i) for k, n, i, _ in rows]


def check(con, key: str, owner_key: str, owner_name: str, issuer: str, fec_ids: dict, budget: list) -> int:
    nm, word = split_name(owner_name), company_word(issuer)
    found = 0
    if nm and word:
        last, first = nm
        params = [("contributor_name", f"{first} {last}"), ("contributor_employer", word),
                  ("is_individual", "true"), ("per_page", "100"), ("sort", "-contribution_receipt_date")]
        params += [("two_year_transaction_period", str(c)) for c in CYCLES]
        page = {}
        for _ in range(3):           # at most 300 line items per person
            if budget[0] <= 0:
                return -1
            budget[0] -= 1
            data = api("schedules/schedule_a/", key, params + list(page.items()))
            if not data:
                break
            for r in data.get("results", []):
                if (r.get("contributor_last_name") or "").upper() != last:
                    continue
                if not (r.get("contributor_first_name") or "").upper().startswith(first):
                    continue
                if word not in re.sub(r"[^A-Z0-9 ]", " ", (r.get("contributor_employer") or "").upper()).split():
                    continue
                cmte = r.get("committee") or {}
                cands = [c for c in (cmte.get("candidate_ids") or []) if c in fec_ids]
                if not cands:
                    continue      # PACs, parties, conduits, or someone not in Congress now
                con.execute("INSERT OR REPLACE INTO fec_donations VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                    str(r.get("sub_id")), owner_key, fec_ids[cands[0]], cands[0],
                    cmte.get("name") or r.get("committee_id"), (r.get("contribution_receipt_date") or "")[:10],
                    r.get("contribution_receipt_amount"), r.get("contributor_name"),
                    r.get("contributor_employer"), r.get("contributor_occupation"),
                    r.get("pdf_url") or f"https://www.fec.gov/data/receipts/individual-contributions/?contributor_name={first}+{last}&contributor_employer={word}"))
                found += 1
            last_idx = (data.get("pagination") or {}).get("last_indexes")
            if not last_idx or len(data.get("results", [])) < 100:
                break
            page = {k: str(v) for k, v in last_idx.items() if v is not None}
    con.execute("INSERT OR REPLACE INTO fec_checked VALUES (?,?,?,?)",
                (owner_key, owner_name, issuer, dt.date.today().isoformat()))
    con.commit()
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=800, help="max FEC requests this run (a key allows 1,000 an hour)")
    a = ap.parse_args()
    key = os.environ.get("FEC_API_KEY", "").strip()
    if not key:
        print("FEC_API_KEY not set: skipping campaign donations")
        return
    con = connect()
    con.executescript(SCHEMA)
    fec_ids = load_fec_ids(con)
    todo = insiders_to_check(con)
    print(f"FEC: {len(fec_ids)} candidate ids for current members; {len(todo)} insiders to check", flush=True)
    budget, people, gifts = [a.budget], 0, 0
    try:
        for k, name, issuer in todo:
            n = check(con, key, k, name, issuer, fec_ids, budget)
            if n < 0:
                print("FEC request budget used up; the rest waits for the next run.")
                break
            people += 1
            gifts += n
    except RateLimited:
        print("FEC rate limit reached; the rest waits for the next run.")
    except Exception as e:  # the report still builds with what is on file
        print("FEC lookups stopped:", e)
    total = con.execute("SELECT COUNT(*), COUNT(DISTINCT owner_key) FROM fec_donations").fetchone()
    print(f"FEC: checked {people} insiders, {gifts} new donations to members; "
          f"{total[0]} donations from {total[1]} insiders on file", flush=True)


if __name__ == "__main__":
    main()
