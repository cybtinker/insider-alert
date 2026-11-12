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


def load_congress(con, since: str) -> dict:
    """House PTR trades disclosed on or after `since`, one row per trade.
    Scanned paper filings appear as a single row that links to the PDF."""
    con.row_factory = sqlite3.Row
    try:
        filings = list(con.execute(
            "SELECT * FROM congress_filings WHERE filed_date >= ? ORDER BY filed_date DESC", (since,)))
    except sqlite3.OperationalError:
        return {"trades": [], "since": since}
    trades = defaultdict(list)
    for t in con.execute("""SELECT t.* FROM congress_trades t JOIN congress_filings f USING (filing_id)
                            WHERE f.filed_date >= ? ORDER BY t.seq""", (since,)):
        trades[t["filing_id"]].append(t)
    rows = []
    for f in filings:
        base = {"member": f["member"], "state": f["state_dst"] or "", "chamber": f["chamber"],
                "mkey": f"{f['member']}|{(f['state_dst'] or '')[:2]}", "filed": f["filed_date"],
                "url": f["url"]}
        if not trades.get(f["filing_id"]):
            rows.append({**base, "id": f["filing_id"], "paper": True, "tx": "Paper filing",
                         "asset": "Scanned filing. Open the PDF to read it.", "ticker": "",
                         "owner": "", "atype": "", "date": None, "lo": None, "hi": None})
            continue
        for t in trades[f["filing_id"]]:
            rows.append({**base, "id": f"{f['filing_id']}-{t['seq']}", "paper": False,
                         "tx": t["tx_type"], "asset": t["asset"], "ticker": t["ticker"] or "",
                         "owner": t["owner"], "atype": t["asset_type"] or "",
                         "date": t["trade_date"], "lo": t["amount_min"], "hi": t["amount_max"]})
    rows += load_senate(since)
    rows.sort(key=lambda r: r["filed"] or "", reverse=True)
    return {"trades": rows, "since": since}


def load_senate(since: str) -> list[dict]:
    """Senate PTRs that senate.py (run on a home PC) pushed to data/senate.json."""
    path = os.path.join(HERE, "data", "senate.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        reports = json.load(f).get("reports", {})
    rows = []
    for rid, r in reports.items():
        if not r.get("filed") or r["filed"] < since:
            continue
        base = {"member": r["member"], "state": "", "chamber": "Senate",
                "mkey": f"{r['member']}|Senate", "filed": r["filed"], "url": r["url"]}
        if r.get("paper") or not r.get("trades"):
            rows.append({**base, "id": "S" + rid, "paper": True, "tx": "Paper filing",
                         "asset": "Scanned filing. Open it to read it.", "ticker": "", "owner": "",
                         "atype": "", "date": None, "lo": None, "hi": None})
            continue
        for t in r["trades"]:
            rows.append({**base, "id": f"S{rid}-{t['seq']}", "paper": False, "tx": t["tx"],
                         "asset": t["asset"], "ticker": t["ticker"], "owner": t["owner"],
                         "atype": t["atype"], "date": t["date"], "lo": t["lo"], "hi": t["hi"]})
    return rows


# ------------------------------------------------------------ committee context

# Which industries each full committee oversees, as SEC SIC code ranges.
# Broad committees (Appropriations, Judiciary, Oversight...) are left out
# because they would match almost everything.
_DEFENSE = [(3480, 3489), (3720, 3729), (3730, 3732), (3760, 3769), (3795, 3795), (3812, 3812)]
_HEALTH = [(2833, 2836), (3841, 3845), (5122, 5122), (5912, 5912), (6324, 6324), (8000, 8099)]
_FINANCE = [(6000, 6799)]
_FARM = [(100, 999), (2000, 2099), (2870, 2879), (3523, 3523), (5150, 5159)]
_ENERGY = [(1000, 1499), (2900, 2999), (4610, 4619), (4900, 4991)]
_TELECOM_TECH = [(3570, 3579), (3660, 3679), (4800, 4899), (7370, 7379)]
_TRANSPORT = [(1600, 1629), (3710, 3716), (3720, 3729), (3740, 3743), (4000, 4799)]
JURISDICTION = {
    "HSAG": _FARM, "SSAF": _FARM,
    "HSAS": _DEFENSE, "SSAS": _DEFENSE,
    "HSBA": _FINANCE, "SSBK": _FINANCE,
    "HSIF": _HEALTH + _ENERGY + _TELECOM_TECH,
    "HSHM": _DEFENSE + [(7370, 7379)], "SSGA": [(3812, 3812), (7370, 7379)],
    "HSII": [(1000, 1499), (2400, 2499)],
    "HSPW": _TRANSPORT + [(4940, 4959)], "SSEV": [(1600, 1629), (4940, 4959), (4900, 4991)],
    "HSSY": [(3720, 3729), (3760, 3769), (3670, 3679)],
    "HSWM": [(6300, 6411), (8000, 8099)], "SSFI": [(2833, 2836), (6300, 6411), (8000, 8099)],
    "HSVR": [(8000, 8099)], "SSVA": [(8000, 8099)],
    "SSCM": _TRANSPORT + _TELECOM_TECH + [(3760, 3769)],
    "SSEG": _ENERGY, "SSHR": _HEALTH,
}
LATE_DAYS = 45            # STOCK Act: report within 45 days of the trade
WINDOW_BEFORE, WINDOW_AFTER = 14, 3   # trade up to 14 days before, or 3 after, a markup or bill meeting


def _covers(code: str, sic) -> bool:
    return sic is not None and any(lo <= sic <= hi for lo, hi in JURISDICTION.get(code[:4], ()))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z ]", "", (s or "").lower().replace("-", " "))


def add_context(con, rows: list[dict]) -> dict:
    """Mark late filings, and link each trade to the member's committees:
    'overlap' when a committee they sit on oversees the company's industry,
    'timing' when that committee also met within days of the trade."""
    import datetime as _dt
    for r in rows:
        if r.get("date") and r.get("filed"):
            lag = (_dt.date.fromisoformat(r["filed"]) - _dt.date.fromisoformat(r["date"])).days
            if lag > LATE_DAYS:
                r["late"] = lag
    try:
        people = [dict(zip(("bio", "name", "first", "last", "chamber", "state", "district"), x))
                  for x in con.execute("SELECT * FROM legislators")]
        seats = defaultdict(list)
        for bio, code, name, title in con.execute("SELECT * FROM committee_seats"):
            seats[bio].append((code, name, title))
        sic = {t: (s, d) for t, s, d in con.execute("SELECT ticker, sic, sic_desc FROM ticker_industry")}
        meetings = defaultdict(list)
        for eid, ch, date, typ, status, title, codes, bills, url in con.execute(
                "SELECT * FROM committee_meetings WHERE date != ''"):
            if (status or "").lower() in ("canceled", "cancelled", "postponed"):
                continue
            for c in {c[:4] for c in json.loads(codes)}:
                meetings[c].append({"date": date, "type": typ or "Meeting", "title": title,
                                    "bills": json.loads(bills), "url": url, "code": c})
    except sqlite3.OperationalError:
        return {"members": 0, "meetings": 0}
    house = {(p["state"], p["district"]): p for p in people if p["chamber"] == "House"}
    senate = [p for p in people if p["chamber"] == "Senate"]

    def who(r):
        if r["chamber"] == "House":
            m = re.match(r"([A-Z]{2})(\d+)", r.get("state") or "")
            if m:
                p = house.get((m.group(1), int(m.group(2))))
                if p and _norm(p["last"]).split()[-1] in _norm(r["member"]):
                    return p
            return None
        name = _norm(r["member"])
        hits = [p for p in senate if _norm(p["last"]) and re.search(r"\b" + _norm(p["last"]) + r"\b", name)]
        if len(hits) > 1:
            hits = [p for p in hits if _norm(p["first"]) and _norm(p["first"]) in name] or hits[:0]
        return hits[0] if len(hits) == 1 else None

    full_names = {}
    for bio, lst in seats.items():
        for code, name, _ in lst:
            if len(code) == 4:
                full_names[code] = name
    cache = {}
    for r in rows:
        if r.get("paper") or not r.get("ticker") or r["ticker"] not in sic:
            continue
        key = r["mkey"]
        if key not in cache:
            cache[key] = who(r)
        p = cache[key]
        if not p:
            continue
        s, desc = sic[r["ticker"]]
        mine = {c[:4] for c, _, _ in seats.get(p["bio"], [])}
        over = sorted(c for c in mine if _covers(c, s))
        if not over:
            continue
        ctx = {"cmte": [full_names.get(c, c) for c in over], "ind": (desc or "").title(), "meet": []}
        if r.get("date"):
            td = _dt.date.fromisoformat(r["date"])
            near = []
            for c in over:
                for m in meetings.get(c, ()):
                    gap = (_dt.date.fromisoformat(m["date"]) - td).days
                    # Only markups and meetings that took up a bill: committees hold
                    # general hearings so often that nearly every trade would match.
                    if -WINDOW_AFTER <= gap <= WINDOW_BEFORE and (m["type"] == "Markup" or m["bills"]):
                        near.append({**m, "days": gap, "cmte": full_names.get(c, c)})
            # Markups and hearings with bills first, then the closest in time.
            near.sort(key=lambda m: (m["type"] != "Markup", not m["bills"], abs(m["days"])))
            ctx["meet"] = [{k: m[k] for k in ("date", "type", "title", "bills", "url", "days", "cmte")}
                           for m in near[:3]]
        r["ctx"] = ctx
        r["flag"] = "timing" if ctx["meet"] else "overlap"
    return {"members": len(people), "meetings": sum(len(v) for v in meetings.values())}


# ------------------------------------------------------------ cross-signals

def cross_signals(trades, holders, congress_rows, end: str, days: int = 30) -> list[dict]:
    """Tickers where two or more independent sources point the same way in
    the last `days` days: company insiders, 5% holders (13D), and Congress."""
    import datetime as _dt
    start = (_dt.date.fromisoformat(end) - _dt.timedelta(days=days)).isoformat()
    by = defaultdict(lambda: {"buy": defaultdict(list), "sell": defaultdict(list), "issuer": ""})
    for t in trades:
        if not t["ticker"] or t["deriv"] or t["suspect"] or not (start <= t["filed"] <= end):
            continue
        e = by[t["ticker"]]
        e["issuer"] = e["issuer"] or t["issuer"]
        if t["code"] == "P" and t["value"] >= 25_000:
            e["buy"]["insider"].append({"who": t["person"], "role": t["role"], "value": t["value"], "date": t["filed"]})
        elif t["code"] == "S" and t["value"] >= 1_000_000 and not t["plan"]:
            e["sell"]["insider"].append({"who": t["person"], "role": t["role"], "value": t["value"], "date": t["filed"]})
    for h in holders:
        if h["ticker"] and "13D" in h["form"] and start <= h["filed"] <= end:
            e = by[h["ticker"]]
            e["issuer"] = e["issuer"] or h["issuer"]
            e["buy"]["fund"].append({"who": h["filer"], "pct": h["pct"], "date": h["filed"], "url": h["url"]})
    for r in congress_rows:
        if r.get("paper") or not r.get("ticker") or not (start <= r["filed"] <= end):
            continue
        side = "buy" if r["tx"] == "Buy" else "sell" if re.search("Sell|Partial", r["tx"] or "") else None
        if side:
            by[r["ticker"]][side]["congress"].append(
                {"who": r["member"], "mkey": r["mkey"], "lo": r["lo"], "hi": r["hi"], "date": r["filed"],
                 "flag": r.get("flag")})
    out = []
    for tk, e in by.items():
        for side in ("buy", "sell"):
            src = {k: v for k, v in e[side].items() if v}
            if len(src) < 2:
                continue
            people = sum(len({x["who"] for x in v}) for v in src.values())
            out.append({"ticker": tk, "issuer": e["issuer"], "side": side, "sources": src,
                        "n": len(src), "people": people,
                        "last": max(x["date"] for v in src.values() for x in v)})
    out.sort(key=lambda x: x["last"], reverse=True)
    out.sort(key=lambda x: (-x["n"], -x["people"]))
    return out[:24]


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
    since = (_dt.date.today() - _dt.timedelta(days=400)).isoformat()
    congress = load_congress(con, since)
    ctx_stats = add_context(con, congress["trades"])
    congress["ctx"] = {**ctx_stats, "late_days": LATE_DAYS,
                       "window": [WINDOW_BEFORE, WINDOW_AFTER]}
    signals = cross_signals(trades, holders, congress["trades"], days[-1])
    preview = {"days": pdays, "generated": generated, "byDay": {d: by_day[d] for d in pdays},
               "congress": congress, "signals": signals}
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
        json.dump({"days": days, "generated": generated, "signals": signals}, f, separators=(",", ":"))
    with open(os.path.join(site, "congress.json"), "w", encoding="utf-8") as f:
        json.dump({**congress, "generated": generated}, f, separators=(",", ":"))
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
    print(f"{len(congress['trades'])} congressional trade rows since {since}; "
          f"{sum(1 for r in congress['trades'] if r.get('flag') == 'timing')} timing flags, "
          f"{sum(1 for r in congress['trades'] if r.get('flag') == 'overlap')} industry overlaps, "
          f"{sum(1 for r in congress['trades'] if r.get('late'))} late filings; {len(signals)} cross-signals")
    print(f"{len(trades)} trades, {len(holders)} ownership filings across {len(days)} days "
          f"({days[0]} to {days[-1]}) -> {site}")


if __name__ == "__main__":
    main()
