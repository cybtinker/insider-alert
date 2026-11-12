"""Committee context for congressional trades.

Loads three things into insider.db so report.py can put each trade next to
what the member's committees were doing:

  1. Committee assignments for every current member of Congress, from the
     public unitedstates/congress-legislators dataset on GitHub.
  2. Committee hearings, markups and meetings (with the bills they took up)
     from the Congress.gov API. Needs a free key in CONGRESS_API_KEY; without
     it this step is skipped and only industry overlap is shown.
  3. The SEC industry code (SIC) of every ticker members traded, from EDGAR.

Usage: CONGRESS_API_KEY=... SEC_USER_AGENT="InsiderAlert you@example.com" python committees.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from collector import connect, fetch

LEGIS = "https://raw.githubusercontent.com/unitedstates/congress-legislators/main/"
CGOV = "https://api.congress.gov/v3/"

SCHEMA = """
CREATE TABLE IF NOT EXISTS legislators (
    bioguide  TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    first     TEXT,
    last      TEXT NOT NULL,
    chamber   TEXT NOT NULL,      -- House / Senate
    state     TEXT NOT NULL,
    district  INTEGER
);
CREATE TABLE IF NOT EXISTS committee_seats (
    bioguide  TEXT NOT NULL,
    code      TEXT NOT NULL,      -- HSAS (full committee) or HSAS25 (subcommittee)
    name      TEXT NOT NULL,
    title     TEXT,               -- Chairman, Ranking Member...
    PRIMARY KEY (bioguide, code)
);
CREATE TABLE IF NOT EXISTS committee_meetings (
    event_id  TEXT PRIMARY KEY,
    chamber   TEXT,
    date      TEXT,               -- YYYY-MM-DD
    type      TEXT,               -- Hearing / Markup / Meeting
    status    TEXT,
    title     TEXT,
    codes     TEXT,               -- JSON list of committee codes, e.g. ["HSAS","HSAS25"]
    bills     TEXT,               -- JSON list of {"label","url"}
    url       TEXT
);
CREATE TABLE IF NOT EXISTS ticker_industry (
    ticker    TEXT PRIMARY KEY,
    cik       TEXT,
    sic       INTEGER,
    sic_desc  TEXT,
    checked   TEXT NOT NULL
);
"""


# ------------------------------------------------------------ assignments

def _yaml(name: str):
    import yaml
    raw = fetch(LEGIS + name)
    if not raw:
        raise SystemExit(f"Couldn't download {name}")
    return yaml.load(raw, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))


def load_assignments(con) -> None:
    people = _yaml("legislators-current.yaml")
    committees = _yaml("committees-current.yaml")
    seats = _yaml("committee-membership-current.yaml")
    names = {}
    for c in committees:
        names[c["thomas_id"]] = c["name"]
        for s in c.get("subcommittees") or []:
            names[c["thomas_id"] + s["thomas_id"]] = f"{c['name']}: {s['name']} subcommittee"
    con.execute("DELETE FROM legislators")
    con.execute("DELETE FROM committee_seats")
    for p in people:
        t = p["terms"][-1]
        con.execute("INSERT OR REPLACE INTO legislators VALUES (?,?,?,?,?,?,?)", (
            p["id"]["bioguide"], p["name"].get("official_full") or f"{p['name']['first']} {p['name']['last']}",
            p["name"].get("first"), p["name"]["last"], "Senate" if t["type"] == "sen" else "House",
            t["state"], t.get("district")))
    for code, members in seats.items():
        for m in members or []:
            if m.get("bioguide"):
                con.execute("INSERT OR REPLACE INTO committee_seats VALUES (?,?,?,?)",
                            (m["bioguide"], code, names.get(code, code), m.get("title")))
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM committee_seats").fetchone()[0]
    print(f"Committee seats: {n} for {len(people)} members", flush=True)


# ------------------------------------------------------------ meetings

def cgov(path: str, key: str, **params) -> dict | None:
    url = CGOV + path + "?" + urlencode({**params, "format": "json", "api_key": key})
    for attempt in range(4):
        time.sleep(0.75)  # Congress.gov allows 5,000 requests an hour
        try:
            with urlopen(Request(url, headers={"User-Agent": "InsiderAlert"}), timeout=30) as r:
                return json.loads(r.read())
        except HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(10 * (attempt + 1))
                continue
            raise
        except (URLError, ConnectionError, TimeoutError):
            if attempt < 3:
                time.sleep(10 * (attempt + 1))
                continue
            raise
    return None


def congress_number(d: dt.date) -> int:
    # A Congress starts on January 3 of each odd year; the 119th began in 2025.
    y = d.year - (1 if d.year % 2 == 0 or (d.month == 1 and d.day < 3) else 0)
    return 119 + (y - 2025) // 2


BILL_KINDS = {"HR": ("H.R.", "house-bill"), "S": ("S.", "senate-bill"),
              "HRES": ("H.Res.", "house-resolution"), "SRES": ("S.Res.", "senate-resolution"),
              "HJRES": ("H.J.Res.", "house-joint-resolution"), "SJRES": ("S.J.Res.", "senate-joint-resolution"),
              "HCONRES": ("H.Con.Res.", "house-concurrent-resolution"),
              "SCONRES": ("S.Con.Res.", "senate-concurrent-resolution")}


def bill_label(b: dict) -> str:
    kind = str(b.get("type") or "").upper().replace(".", "")
    return f"{BILL_KINDS.get(kind, (kind,))[0]} {b.get('number', '')}".strip()


def bill_url(b: dict) -> str:
    kind = str(b.get("type") or "").upper().replace(".", "")
    path = BILL_KINDS.get(kind, (None, "house-bill" if kind.startswith("H") else "senate-bill"))[1]
    return f"https://www.congress.gov/bill/{b.get('congress')}th-congress/{path}/{b.get('number')}"


def load_meetings(con, key: str, budget: int) -> None:
    have = {r[0] for r in con.execute("SELECT event_id FROM committee_meetings")}
    today = dt.date.today()
    congresses = sorted({congress_number(today), congress_number(today - dt.timedelta(days=400))})
    todo = []
    for cong in congresses:
        for chamber in ("house", "senate"):
            offset = 0
            while True:
                page = cgov(f"committee-meeting/{cong}/{chamber}", key, limit=250, offset=offset)
                items = (page or {}).get("committeeMeetings") or []
                todo += [(cong, chamber, str(m["eventId"])) for m in items if str(m.get("eventId")) not in have]
                if len(items) < 250:
                    break
                offset += 250
    print(f"Committee meetings: {len(todo)} new, fetching up to {budget}", flush=True)
    for i, (cong, chamber, eid) in enumerate(todo[:budget]):
        d = (cgov(f"committee-meeting/{cong}/{chamber}/{eid}", key) or {}).get("committeeMeeting")
        if not d:
            continue
        codes = sorted({re.sub(r"00$", "", (c.get("systemCode") or "").upper())
                        for c in d.get("committees") or [] if c.get("systemCode")})
        bills = [{"label": bill_label(b), "url": bill_url(b)}
                 for b in (d.get("relatedItems") or {}).get("bills") or []]
        con.execute("INSERT OR REPLACE INTO committee_meetings VALUES (?,?,?,?,?,?,?,?,?)", (
            eid, chamber.title(), (d.get("date") or "")[:10], d.get("type"), d.get("meetingStatus"),
            (d.get("title") or "").strip(), json.dumps(codes), json.dumps(bills),
            f"https://www.congress.gov/event/{cong}th-congress/{chamber}-event/{eid}"))
        if i % 100 == 99:
            con.commit()
            print(f"  {i + 1}", flush=True)
    con.commit()


# ------------------------------------------------------------ industries

def load_industries(con) -> None:
    con.row_factory = None
    wanted = {r[0] for r in con.execute(
        "SELECT DISTINCT ticker FROM congress_trades WHERE ticker IS NOT NULL AND ticker != ''")}
    senate = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "senate.json")
    if os.path.exists(senate):
        with open(senate, encoding="utf-8") as f:
            for r in json.load(f).get("reports", {}).values():
                wanted |= {t["ticker"] for t in r.get("trades") or [] if t.get("ticker")}
    stale = (dt.date.today() - dt.timedelta(days=90)).isoformat()
    known = {r[0] for r in con.execute("SELECT ticker FROM ticker_industry WHERE checked >= ?", (stale,))}
    todo = sorted(wanted - known)
    if not todo:
        return
    raw = fetch("https://www.sec.gov/files/company_tickers.json")
    ciks = {v["ticker"].upper(): str(v["cik_str"]) for v in json.loads(raw).values()} if raw else {}
    print(f"Industry codes: {len(todo)} tickers to look up", flush=True)
    now = dt.date.today().isoformat()
    for tk in todo:
        cik = ciks.get(tk.upper()) or ciks.get(tk.upper().replace(".", "-"))
        sic, desc = None, None
        if cik:
            try:
                sub = fetch(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json")
                if sub:
                    j = json.loads(sub)
                    sic = int(j["sic"]) if str(j.get("sic") or "").isdigit() else None
                    desc = j.get("sicDescription")
            except Exception as e:  # one bad lookup shouldn't stop the run
                print("  skip", tk, e)
                continue
        con.execute("INSERT OR REPLACE INTO ticker_industry VALUES (?,?,?,?,?)", (tk, cik, sic, desc, now))
    con.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=3000, help="max meeting detail requests this run")
    a = ap.parse_args()
    con = connect()
    con.executescript(SCHEMA)
    load_assignments(con)
    load_industries(con)
    key = os.environ.get("CONGRESS_API_KEY", "").strip()
    if key:
        try:
            load_meetings(con, key, a.budget)
        except Exception as e:  # the report still builds without meetings
            print("Committee meetings skipped:", e)
    else:
        print("CONGRESS_API_KEY not set: skipping committee meetings")


if __name__ == "__main__":
    main()
