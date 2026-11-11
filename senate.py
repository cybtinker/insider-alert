"""Senate stock trades, run from a home PC.

The Senate's eFD site (efdsearch.senate.gov) refuses requests from cloud
servers, so this script runs on your own computer once a day. It accepts the
site's usage terms (personal, non-commercial use), downloads new Periodic
Transaction Reports, saves them to data/senate.json, and with --push commits
and pushes that file so the website rebuilds.

Usage (from the repo folder):
    python senate.py              # update data/senate.json
    python senate.py --push       # ...and push it to GitHub
    python senate.py --days 400   # first run: about 13 months of history
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("Install requests first:  python -m pip install requests")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "senate.json")
BASE = "https://efdsearch.senate.gov"
CONTACT = os.environ.get("SEC_USER_AGENT", "InsiderAlert personal research tool")
PAUSE = 1.0  # seconds between requests, to be polite


def strip(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


class EFD:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = CONTACT

    def get(self, path, **kw):
        time.sleep(PAUSE)
        r = self.s.get(BASE + path, timeout=60, **kw)
        r.raise_for_status()
        return r

    def agree(self):
        r = self.get("/search/home/")
        tok = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', r.text)
        if not tok:
            raise RuntimeError("Couldn't find the Senate site's agreement form.")
        time.sleep(PAUSE)
        r = self.s.post(BASE + "/search/home/", timeout=60,
                        data={"prohibition_agreement": "1", "csrfmiddlewaretoken": tok.group(1)},
                        headers={"Referer": BASE + "/search/home/"})
        if r.status_code == 403:
            raise RuntimeError("The Senate site refused this connection (403). "
                               "It blocks some networks, such as VPNs and cloud servers.")
        r.raise_for_status()

    def search(self, since: dt.date):
        """Yield PTR search rows submitted on or after `since`."""
        csrf = self.s.cookies.get("csrftoken")
        start = 0
        while True:
            time.sleep(PAUSE)
            r = self.s.post(BASE + "/search/report/data/", timeout=60, data={
                "start": str(start), "length": "100", "report_types": "[11]", "filer_types": "[]",
                "submitted_start_date": since.strftime("%m/%d/%Y 00:00:00"), "submitted_end_date": "",
                "candidate_state": "", "senator_state": "", "office_id": "", "first_name": "",
                "last_name": "", "csrfmiddlewaretoken": csrf},
                headers={"Referer": BASE + "/search/", "X-CSRFToken": csrf})
            r.raise_for_status()
            j = r.json()
            rows = j.get("data", [])
            for row in rows:
                yield row
            start += len(rows)
            if not rows or start >= int(j.get("recordsFiltered", j.get("recordsTotal", 0))):
                break


def money_range(s: str):
    nums = [int(x.replace(",", "")) for x in re.findall(r"\$([\d,]+)", s or "")]
    if not nums:
        return None, None
    if s.strip().lower().startswith("over"):
        return nums[0], None
    return nums[0], (nums[1] if len(nums) > 1 else None)


def iso(mdy: str):
    try:
        return dt.datetime.strptime(mdy.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


TX = {"purchase": "Buy", "sale (full)": "Sell", "sale (partial)": "Partial sale",
      "sale": "Sell", "exchange": "Exchange"}


def parse_ptr(page: str) -> list[dict]:
    """Parse the transactions table on an electronic PTR page."""
    m = re.search(r"<table[^>]*>(.*?)</table>", page, re.S | re.I)
    if not m:
        return []
    table = m.group(1)
    heads = [strip(h).lower() for h in re.findall(r"<th[^>]*>(.*?)</th>", table, re.S | re.I)]
    out = []
    for i, tr in enumerate(re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S | re.I)):
        cells = [strip(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
        if not cells or len(cells) < 6:
            continue
        row = dict(zip(heads, cells)) if len(heads) == len(cells) else {}
        def col(*names):
            for n in names:            # exact header first, then partial match
                if n in row:
                    return row[n]
            return next((row[k] for n in names for k in row if n in k), "")
        ticker = col("ticker")
        tx = col("type")
        if not tx or tx.lower().startswith(("stock", "other", "corporate", "municipal")):
            tx = next((c for c in cells if c.lower().split(" (")[0] in ("purchase", "sale", "exchange")), tx)
        lo, hi = money_range(col("amount"))
        out.append({
            "seq": i,
            "date": iso(col("transaction date", "date")),
            "owner": col("owner").title() or "Self",
            "ticker": "" if ticker in ("--", "") else ticker.split()[0].upper(),
            "asset": col("asset name", "asset"),
            "atype": col("asset type"),
            "tx": TX.get(tx.lower(), tx),
            "lo": lo, "hi": hi,
            "comment": "" if col("comment") in ("--", "") else col("comment"),
        })
    return out


def load() -> dict:
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            return json.load(f)
    return {"reports": {}, "updated": None}


def save(db: dict):
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    db["updated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=1, sort_keys=True)


def git_push():
    def run(*a):
        return subprocess.run(["git", *a], cwd=HERE, capture_output=True, text=True)
    run("pull", "--rebase", "--autostash", "origin", "main")
    run("add", "data/senate.json")
    if not run("diff", "--cached", "--quiet").returncode:
        print("No new Senate filings to push.")
        return
    run("commit", "-m", "Update Senate trades")
    r = run("push", "origin", "HEAD:main")
    print("Pushed." if r.returncode == 0 else "Push failed:\n" + r.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60, help="look back this many days for new filings")
    ap.add_argument("--push", action="store_true", help="commit and push data/senate.json")
    a = ap.parse_args()
    db = load()
    efd = EFD()
    efd.agree()
    since = dt.date.today() - dt.timedelta(days=a.days)
    new = 0
    for row in efd.search(since):
        link = re.search(r'href="([^"]+)"', row[3] if len(row) > 3 else "")
        if not link:
            continue
        path = link.group(1)
        rid = path.rstrip("/").split("/")[-1]
        if rid in db["reports"]:
            continue
        first, last = strip(row[0]), strip(row[1])
        rep = {"member": f"{first} {last}".strip(), "office": strip(row[2]),
               "title": strip(row[3]), "filed": iso(strip(row[4])) if len(row) > 4 else None,
               "url": BASE + path, "paper": "/paper/" in path, "trades": []}
        if not rep["paper"]:
            try:
                rep["trades"] = parse_ptr(efd.get(path).text)
            except requests.HTTPError as e:
                print("  skipped", path, e)
                continue
        db["reports"][rid] = rep
        new += 1
        print(f"  {rep['filed']}  {rep['member']}: {len(rep['trades'])} trades", flush=True)
    save(db)
    print(f"{new} new Senate reports. {len(db['reports'])} saved in {OUT}")
    if a.push:
        git_push()


if __name__ == "__main__":
    main()
