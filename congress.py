"""Congressional stock trades (STOCK Act Periodic Transaction Reports).

Senate: efdsearch.senate.gov refuses automated clients (HTTP 403 from its
CDN, tested from both a cloud container and GitHub Actions in Nov 2026),
so only the House is collected for now.

House: the Clerk publishes a yearly index (YEARFD.zip) of every disclosure;
filings of type "P" are PTRs, each a PDF. Electronically filed PDFs have a
text layer we parse; scanned paper filings are kept as links only.

Usage: SEC_USER_AGENT="InsiderAlert you@example.com" python congress.py [--year 2026]
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import re
import sqlite3
import zipfile

from collector import connect, fetch

HOUSE = "https://disclosures-clerk.house.gov/public_disc/"

SCHEMA = """
CREATE TABLE IF NOT EXISTS congress_filings (
    filing_id   TEXT PRIMARY KEY,      -- chamber + doc id
    chamber     TEXT NOT NULL,         -- House / Senate
    member      TEXT NOT NULL,
    state_dst   TEXT,
    filed_date  TEXT NOT NULL,
    url         TEXT NOT NULL,
    parsed      INTEGER NOT NULL,      -- 1 text parsed, 0 scanned/unreadable
    fetched_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS congress_trades (
    filing_id   TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    owner       TEXT,                  -- Self, Spouse, Joint, Dependent
    asset       TEXT,
    ticker      TEXT,
    asset_type  TEXT,                  -- ST stock, OP option, GS govt security...
    tx_type     TEXT,                  -- Buy / Sell / Partial sale / Exchange
    trade_date  TEXT,
    notified    TEXT,
    amount_min  INTEGER,
    amount_max  INTEGER,
    PRIMARY KEY (filing_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_cf_date ON congress_filings(filed_date);
CREATE INDEX IF NOT EXISTS ix_ct_ticker ON congress_trades(ticker);
"""

OWNERS = {"SP": "Spouse", "JT": "Joint", "DC": "Dependent", "SELF": "Self"}
TX = {"P": "Buy", "S": "Sell", "S (partial)": "Partial sale", "E": "Exchange"}
ROW = re.compile(r"\s(P|S \(partial\)|S|E)\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+"
                 r"(Over \$[\d,]+|\$[\d,]+(?:\s*-\s*(?:\$[\d,]+)?)?)")
META_LINE = re.compile(r"^[A-Z](?:\s+[A-Z])?\s*:\s")   # "F S : New", "S O : Schwab", "D : ..."
FILING_STATUS = re.compile(r"^F\s+S\s*:")
HEADER_BITS = ("ID Owner Asset", "Type Date Gains", "$200?", "Filing ID #")


def iso(mdy: str) -> str | None:
    try:
        m, d, y = (int(x) for x in mdy.split("/"))
        return dt.date(y, m, d).isoformat()
    except (ValueError, AttributeError):
        return None


def money(s: str) -> int | None:
    m = re.search(r"[\d,]+", s or "")
    return int(m.group(0).replace(",", "")) if m else None


def parse_ptr_text(text: str) -> list[dict]:
    """Turn the text of an e-filed House PTR into transactions."""
    text = text.replace("\x00", "")
    start = text.find("$200?")
    end = text.find("* For the complete list")
    if start < 0:
        return []
    body = text[start + 5:end if end > 0 else None].splitlines()
    blocks, cur = [], []
    for line in body:
        line = line.strip()
        if not line or any(h in line for h in HEADER_BITS):
            continue
        if FILING_STATUS.match(line):
            if cur:
                blocks.append(" ".join(cur))
            cur = []
            continue
        if META_LINE.match(line):
            continue
        cur.append(line)
    if cur:
        blocks.append(" ".join(cur))

    out = []
    for i, b in enumerate(blocks):
        m = ROW.search(" " + b)
        if not m:
            continue
        b = " " + b
        tx, tdate, ndate, amt = m.groups()
        rest = (b[:m.start()] + " " + b[m.end():]).strip()
        lo, hi = None, None
        if amt.startswith("Over"):
            lo = money(amt)
        else:
            lo = money(amt)
            nums = re.findall(r"\$[\d,]+", amt)
            if len(nums) > 1:
                hi = money(nums[1])
            elif amt.rstrip().endswith("-"):
                m2 = re.search(r"\$[\d,]+", rest)   # upper bound wrapped onto the next line
                if m2:
                    hi = money(m2.group(0))
                    rest = (rest[:m2.start()] + rest[m2.end():]).strip()
        owner = "Self"
        om = re.match(r"^(SP|JT|DC)\s+", rest)
        if om:
            owner = OWNERS[om.group(1)]
            rest = rest[om.end():]
        tk = re.search(r"\(([A-Z][A-Z0-9.\-]{0,7})\)", rest)
        at = re.search(r"\[([A-Z]{2})\]", rest)
        asset = re.sub(r"\([A-Z][A-Z0-9.\-]{0,7}\)|\[[A-Z]{2}\]", "", rest)
        asset = re.sub(r"\s+-\s+Common Stock|\s+Common Stock", "", asset)
        asset = re.sub(r"\s+", " ", asset).strip(" -")
        out.append({"seq": i, "owner": owner, "asset": asset,
                    "ticker": tk.group(1) if tk else None,
                    "asset_type": at.group(1) if at else None,
                    "tx": TX.get(tx, tx), "trade_date": iso(tdate), "notified": iso(ndate),
                    "lo": lo, "hi": hi})
    return out


def pdf_text(raw: bytes) -> str:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


def house_index(year: int) -> list[dict]:
    raw = fetch(f"{HOUSE}financial-pdfs/{year}FD.zip")
    if not raw:
        return []
    z = zipfile.ZipFile(io.BytesIO(raw))
    xml = z.read(f"{year}FD.xml").decode("utf-8-sig")
    rows = []
    for m in re.findall(r"<Member>(.*?)</Member>", xml, re.S):
        g = lambda t: (re.search(f"<{t}>(.*?)</{t}>", m) or [None, ""])[1].strip()
        if g("FilingType") != "P":
            continue
        first = re.sub(r"\b(Mr|Mrs|Ms|Dr|Hon)\.?(\s|$)", " ", g("First"))
        name = re.sub(r"\s+", " ", " ".join(x for x in (first, g("Last"), g("Suffix")) if x)).strip()
        rows.append({"doc": g("DocID"), "member": name, "state": g("StateDst"),
                     "filed": iso(g("FilingDate")), "year": year})
    return rows


def collect_house(con, year: int, since: str | None = None) -> int:
    have = {r[0] for r in con.execute("SELECT filing_id FROM congress_filings WHERE chamber='House'")}
    todo = [r for r in house_index(year)
            if f"H{r['doc']}" not in have and r["filed"] and (not since or r["filed"] >= since)]
    print(f"House {year}: {len(todo)} new PTRs", flush=True)
    n = 0
    for r in todo:
        url = f"{HOUSE}ptr-pdfs/{year}/{r['doc']}.pdf"
        try:
            raw = fetch(url)
        except Exception as e:  # keep going past one bad file
            print("  skip", r["doc"], e)
            continue
        if not raw:
            continue
        try:
            trades = parse_ptr_text(pdf_text(raw))
        except Exception:
            trades = []
        fid = f"H{r['doc']}"
        con.execute("INSERT OR REPLACE INTO congress_filings VALUES (?,?,?,?,?,?,?,?)",
                    (fid, "House", r["member"], r["state"], r["filed"], url, 1 if trades else 0,
                     dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
        for t in trades:
            con.execute("INSERT OR REPLACE INTO congress_trades VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (fid, t["seq"], t["owner"], t["asset"], t["ticker"], t["asset_type"],
                         t["tx"], t["trade_date"], t["notified"], t["lo"], t["hi"]))
        n += 1
        if n % 50 == 0:
            con.commit()
            print(f"  {n}/{len(todo)}", flush=True)
    con.commit()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=dt.date.today().year)
    ap.add_argument("--since", help="only filings on or after YYYY-MM-DD")
    a = ap.parse_args()
    con = connect()
    con.executescript(SCHEMA)
    collect_house(con, a.year, a.since)
    # Keep about 13 months of history: last year's filings back to that point.
    floor = (dt.date.today() - dt.timedelta(days=400)).isoformat()
    collect_house(con, a.year - 1, max(a.since or floor, floor))


if __name__ == "__main__":
    main()
