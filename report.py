"""Insider Alert report builder.

Reads insider.db (filled by collector.py) and writes:
  out/insider-alert.html   one self-contained report page
  out/trades-YYYY-MM-DD.csv the latest day's trades
  site/                     the installable website (index.html, index.json, days/*.json)

Usage: python report.py [--days 10]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("INSIDER_DB", os.path.join(HERE, "insider.db"))
OUT = os.path.join(HERE, "out")

CODE_LABEL = {
    "P": "Buy", "S": "Sell", "A": "Grant", "M": "Option exercise", "F": "Tax withholding",
    "G": "Gift", "C": "Conversion", "X": "Option exercise", "D": "Return to issuer",
    "J": "Other", "W": "Inheritance", "I": "Discretionary", "K": "Swap", "V": "Voluntary",
    "E": "Expiration", "H": "Expiration", "L": "Small acquisition", "Z": "Trust deposit",
    "U": "Tender",
}
TOP_ROLE = re.compile(r"\b(CEO|chief executive|CFO|chief financial|president|chair(man|woman)?|COO|chief operating)\b", re.I)


def role_of(o) -> str:
    parts = []
    if o["officer_title"]:
        parts.append(o["officer_title"])
    elif o["is_officer"]:
        parts.append("Officer")
    if o["is_director"]:
        parts.append("Director")
    if o["is_ten_pct"]:
        parts.append("10% owner")
    return ", ".join(parts) or "Other"


def title_case(name: str) -> str:
    # EDGAR names are "LAST FIRST MIDDLE"; make them readable but keep order.
    if name and name.isupper():
        return " ".join(w.capitalize() if len(w) > 2 else w.title() for w in name.split())
    return name or ""


def load_rows(con, since: str):
    con.row_factory = sqlite3.Row
    owners = defaultdict(list)
    for o in con.execute("""SELECT o.* FROM owners o JOIN filings f USING (accession)
                            WHERE f.filed_date >= ?""", (since,)):
        owners[o["accession"]].append(o)
    q = """
    SELECT f.accession, f.form, f.filed_date, f.issuer_name, f.ticker, f.url,
           t.code, t.is_derivative, MIN(t.trans_date) AS trans_date,
           SUM(t.shares) AS shares, SUM(t.value) AS value,
           MAX(t.plan_10b5_1) AS plan, MAX(t.acq_disp) AS acq_disp,
           MAX(t.shares_after) AS shares_after
    FROM filings f JOIN transactions t USING (accession)
    WHERE f.filed_date >= ? AND f.form IN ('4','4/A')
    GROUP BY f.accession, t.code, t.is_derivative
    """
    trades = []
    for r in con.execute(q, (since,)):
        os_ = owners.get(r["accession"], [])
        if not os_:
            continue
        first = os_[0]
        name = title_case(first["owner_name"])
        if len(os_) > 1:
            name += f" +{len(os_) - 1}"
        value = r["value"] or 0
        shares = r["shares"] or 0
        price = value / shares if shares and value else None
        # Filers sometimes type a price in the wrong unit or currency. Flag it
        # and keep it out of totals and rankings rather than trusting it.
        suspect = bool(price and price > 20000 and not (r["ticker"] or "").startswith("BRK")) or value > 5e9
        trades.append({
            "id": r["accession"] + (r["code"] or "") + str(r["is_derivative"]),
            "acc": r["accession"], "filed": r["filed_date"], "date": r["trans_date"],
            "ticker": r["ticker"] or "", "issuer": r["issuer_name"] or "",
            "person": name, "pkey": (first["owner_cik"] or first["owner_name"]),
            "role": role_of(first), "top": bool(TOP_ROLE.search(first["officer_title"] or "")),
            "ten": bool(first["is_ten_pct"]),
            "code": r["code"] or "", "type": CODE_LABEL.get(r["code"] or "", r["code"] or "Other"),
            "deriv": bool(r["is_derivative"]), "shares": round(shares),
            "price": round(value / shares, 2) if shares and value else None,
            "value": round(value), "after": r["shares_after"],
            "plan": bool(r["plan"]), "amend": r["form"] == "4/A", "url": r["url"],
            "suspect": suspect,
        })
    holders = [dict(r) for r in con.execute(
        """SELECT accession AS acc, form, filed_date AS filed, issuer_name AS issuer,
                  ticker, filer_name AS filer, percent_class AS pct, url
           FROM filings WHERE filed_date >= ? AND form NOT IN ('4','4/A')
           ORDER BY filed_date DESC""", (since,))]
    for h in holders:
        h["kind"] = "13D (activist)" if "13D" in h["form"] else "13G (passive)"
        h["amend"] = h["form"].endswith("/A")
        h["filer"] = title_case(h["filer"] or "")
    return trades, holders


def score(trades):
    """Flag trades worth a look and explain why."""
    # Cluster buying: distinct insiders buying the same ticker in the 14
    # calendar days up to each filing.
    import datetime as _dt
    buys = defaultdict(list)    # ticker -> [(filed, person)]
    for t in trades:
        if t["code"] == "P" and not t["deriv"] and t["ticker"] and t["value"] >= 25_000:
            buys[t["ticker"]].append((t["filed"], t["pkey"]))

    def cluster(t):
        end = t["filed"]
        start = (_dt.date.fromisoformat(end) - _dt.timedelta(days=14)).isoformat()
        return len({p for f, p in buys.get(t["ticker"], ()) if start <= f <= end})
    for t in trades:
        s, why = 0, []
        if t["deriv"] or t["suspect"]:
            t["score"], t["why"] = 0, []
            continue
        v = t["value"]
        if t["code"] == "P":
            if v >= 100_000:
                s += 40
                why.append("Open-market buy")
            if t["top"]:
                s += 30
                why.append("CEO/CFO/Chair buying")
            if v >= 1_000_000:
                s += 20
                why.append("Over $1M")
            n = cluster(t)
            if n >= 2:
                s += 25
                why.append(f"Cluster: {n} insiders buying")
            if t["ten"] and v >= 1_000_000:
                s += 10
                why.append("10% owner adding")
        elif t["code"] == "S":
            if v >= 10_000_000:
                s += 35
                why.append("Sale over $10M")
            if v >= 10_000_000 and t["top"]:
                s += 15
                why.append("CEO/CFO/Chair selling")
            if v >= 10_000_000 and not t["plan"]:
                s += 5
                why.append("No 10b5-1 plan box ticked")
        t["score"], t["why"] = s, why
    return trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=65, help="filing days to publish (about 3 months)")
    ap.add_argument("--preview-days", type=int, default=10, help="days embedded in the single-file preview")
    a = ap.parse_args()
    import datetime as _dt
    import shutil
    con = sqlite3.connect(DB_PATH)
    days = sorted(r[0] for r in con.execute(
        "SELECT DISTINCT filed_date FROM filings WHERE filed_date IN (SELECT day FROM index_days) "
        "ORDER BY filed_date DESC LIMIT ?", (a.days,)))
    if not days:
        raise SystemExit("No filings in the database yet. Run collector.py first.")
    trades, holders = load_rows(con, days[0])
    score(trades)
    by_day = {d: {"trades": [], "holders": []} for d in days}
    for t in trades:
        if t["filed"] in by_day:
            by_day[t["filed"]]["trades"].append(t)
    for h in holders:
        if h["filed"] in by_day:
            by_day[h["filed"]]["holders"].append(h)
    generated = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tpl = open(os.path.join(HERE, "report_template.html"), encoding="utf-8").read()

    # 1. Single-file preview (Claude artifact): the last few days embedded.
    os.makedirs(OUT, exist_ok=True)
    pdays = days[-a.preview_days:]
    preview = {"days": pdays, "generated": generated, "byDay": {d: by_day[d] for d in pdays}}
    blob = json.dumps(preview, separators=(",", ":")).replace("</", "<\\/")
    with open(os.path.join(OUT, "insider-alert.html"), "w", encoding="utf-8") as f:
        f.write(tpl.replace("/*__DATA__*/null", blob))

    # 2. Website (GitHub Pages): shell, index.json, one JSON file per day, PWA files.
    site = os.path.join(HERE, "site")
    shutil.rmtree(site, ignore_errors=True)
    os.makedirs(os.path.join(site, "days"))
    static = os.path.join(HERE, "site_static")
    for name in os.listdir(static):
        if name != "head.html":
            shutil.copy(os.path.join(static, name), site)
    head = open(os.path.join(static, "head.html"), encoding="utf-8").read()
    with open(os.path.join(site, "index.html"), "w", encoding="utf-8") as f:
        f.write(head + tpl + "\n</body>\n</html>\n")
    with open(os.path.join(site, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"days": days, "generated": generated}, f, separators=(",", ":"))
    for d, payload in by_day.items():
        with open(os.path.join(site, "days", f"{d}.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))

    # 3. CSV of the latest day.
    latest = days[-1]
    with open(os.path.join(OUT, f"trades-{latest}.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["filed", "trade_date", "ticker", "issuer", "insider", "role", "type",
                    "shares", "avg_price", "value_usd", "shares_after", "10b5_1_plan", "sec_link"])
        for t in sorted(by_day[latest]["trades"], key=lambda t: -t["value"]):
            w.writerow([t["filed"], t["date"], t["ticker"], t["issuer"], t["person"], t["role"],
                        t["type"], t["shares"], t["price"], t["value"], t["after"],
                        "yes" if t["plan"] else "", t["url"]])
    print(f"{len(trades)} trades, {len(holders)} ownership filings across {len(days)} days "
          f"({days[0]} to {days[-1]}) -> {site}")


if __name__ == "__main__":
    main()
