# Insider Alert

A daily web app of SEC insider trades (Form 4) and 5% ownership filings (Schedule 13D/13G), for every symbol. It works on desktop and can be added to an iPhone or Android home screen.

## How it works

- `collector.py` downloads each business day's EDGAR form index, then fetches and parses every Form 4 and 13D/13G into `insider.db` (SQLite). It stays under the SEC's 10 requests per second limit.
- `report.py` ranks the trades (open-market buys, CEO/CFO buys, several insiders buying together, sales over $10M) and builds `site/`: `index.html`, `data.json`, and the install files.
- `.github/workflows/daily.yml` runs both on GitHub Actions on weekday nights and mornings, and publishes `site/` to GitHub Pages. The database is kept between runs in the Actions cache. If the cache is lost, the next run downloads the recent days again.

## One-time setup

1. Settings → Pages → Source: **GitHub Actions**.
2. Settings → Secrets and variables → Actions → Secrets → New repository secret (a secret, so it stays out of the public run logs):
   `SEC_USER_AGENT` = `InsiderAlert your-email@example.com` (the SEC requires a contact email).
3. Actions → Daily insider report → Run workflow (the first run backfills about two weeks).

## Run locally

```
SEC_USER_AGENT="InsiderAlert you@example.com" python collector.py --days 14
python report.py
python -m http.server -d site 8000
```
