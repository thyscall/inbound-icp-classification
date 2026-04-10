# Inbound ICP classification (Sonar)

A small Python pipeline that turns a raw list of inbound companies into a scored, CRM-ready CSV. Each company is scraped (or researched via Gemini when the site fails), then classified with **Gemini** using **Sonar ICP context** from `sonar-icp-deep-research.txt`.

## Why it matters

Inbound lists are noisy. Mixed industries, wrong domains, and no consistent read on fit. Sales and GTM ops waste cycles opening sites and debating “is this us?” This system applies one rubric at scale so routing, prioritization, and follow-up start from the same evidence.

## Outcome for sales & GTM

- **Eight-column output** (`classified-companies.csv`): firm type (Law / Financial / Healthcare / Accounting / Creative), ICP fit (Strong / Weak / Not a fit), estimated user count, and short reasons for each.
- **Resumable runs**: already-classified domains are skipped; failures are logged with counts and a terminal preview of the first 15 rows.
- **Flags for review**: weak scrapes fall back to Google Search and domains can be marked with `*` when appropriate.

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the flow diagram.

## Tech stack

| Layer | Choice |
|--------|--------|
| Runtime | Python |
| Data | pandas, pydantic |
| Scrape | Firecrawl |
| Classification | Google Gemini (`google-genai`) |
| Context | `sonar-icp-deep-research.txt` |

## Quick start

```bash
cd inbound-icp-classification
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# .env with FIRECRAWL_API_KEY and GEMINI_API_KEY
python main.py
```

Optional: `python verify_apis.py` to sanity-check keys.

## Inputs / outputs

| File | Role |
|-----------------------------|------------------------------------------------------|
| `raw-inbound-companies.txt` | Alternating company name / domain lines copied from a table in Google Docs|
| `inbound-companies.csv`     | Generated clean list using raw-inbound-companies.txt |
| `classified-companies.csv`  | One row per classified company                       |
