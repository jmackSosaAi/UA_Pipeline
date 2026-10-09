# UA Pipeline

A defense technology startup sourcing prototype for collecting leads, promoting them into a working company table, enriching and scoring them, and reviewing the results in a Streamlit dashboard.

The demo-stable path is:

```text
collector -> raw_leads/canonical_companies -> promote -> companies
          -> enrich -> score/classify -> dashboard
```

The dashboard reads from `data/companies.db`. The file `src/companies.db` is not the active demo database and should not be used for demo runs.

---

## Quick Start

From the repo root:

```bash
source venv/bin/activate
./venv/bin/python -m src.db.migrate --db data/companies.db --verbose
./venv/bin/streamlit run src/dashboard.py
```

Open the sidebar `Preflight` panel first. It shows the active DB path, table counts, key API presence booleans, and warnings for missing or empty datasets.

---

## Architecture

```text
config/
  thesis.yaml                     scoring and investment thesis config
  sources/                        source-specific YAML config

data/
  companies.db                    active SQLite DB for demo and dashboard

src/db/
  migrate.py                      canonical idempotent DB init/migration

src/collectors/
  base.py                         shared raw_leads insertion helpers
  dedup.py                        raw_leads -> canonical_companies matching
  promote.py                      canonical_companies -> companies promotion
  manual.py                       manual lead intake
  defense_press.py                RSS/press collector
  sbir.py                         local SBIR CSV collector
  diana.py                        NATO DIANA live-search collector
  apify_leads.py                  Apify/Apollo paid collector
  enrich_leads.py                 experimental placeholder; not demo path

src/
  enrich.py                       website discovery and Claude extraction
  score.py                        thesis scoring
  classify.py                     category classification
  dossier.py                      company dossier generation
  dashboard.py                    Streamlit dashboard entry point

src/ui/
  data.py                         dashboard DB access
  preflight.py                    sidebar preflight/status panel
  router.py                       navigation/sidebar
  tabs/                           Home, Deal Flow, Press, Portfolio, Pipeline, etc.
```

---

## Project documentation

- `README.md` — how the tool works, demo path, commands (this file).
- `docs/DECISIONS.md` — architectural decisions and their rationale.
  The "why" layer: read this before reverting or replacing something
  load-bearing.
- `docs/QUESTIONS_TO_ANSWER.md` — open strategic questions awaiting
  operator input (fund thesis, scoring thresholds, etc.). Each
  guess in code that should one day be a deliberate choice has a Q-ID.
- `docs/APIFY_ROADMAP.md` — phased plan for the Apify integration
  (discovery → enrichment → employees → post monitoring).
- `docs/SCHEDULING.md` — operational posture for collectors:
  cost-bearing vs. free, manual-run vs. scheduled.

---

## Active Database

Use:

```text
data/companies.db
```

Do not use `src/companies.db` for the demo. It is a legacy/confusing local file path and is not where the dashboard or canonical migration now point.

The canonical migration is safe to run repeatedly:

```bash
./venv/bin/python -m src.db.migrate --db data/companies.db --verbose
```

It creates/verifies required tables and columns without dropping data.

---

## Real Pipeline

1. Collectors write leads to `raw_leads`.
2. Deduplication assigns each raw lead to `canonical_companies`.
3. Promotion materializes canonical companies into `companies`.
4. The dashboard Deal Flow reads from `companies`, not directly from `raw_leads`.
5. Enrichment fills website-derived structured fields and `enriched_at`.
6. Scoring/classification populate score, tier, and category fields.
7. The dashboard displays companies, press mentions, portfolio coverage, and detail pages.

Important visibility rule:

Raw leads do not appear in Deal Flow until promoted into `companies`. Promoted companies are loaded by Deal Flow, but may be hidden by default if they have no description, enrichment, or dossier. Enable `Show unenriched companies` in the sidebar or run enrichment next.

---

## Demo-Safe Sources

Prefer these for a live demo:

| Source | Demo posture | Notes |
|---|---|---|
| Manual intake | Safe if you understand it may research live depending on command/options | Good for controlled single-company demos. |
| Curated articles | Safe if already collected | Use existing DB rows for Press tab demos. |
| Defense Press | Safe if already collected | Avoid live RSS collection during the demo unless preflight is green. |
| SBIR local CSV | Safe if local CSV is present | Local file based; no paid API by itself. |

Use cautiously or offline:

| Source | Risk |
|---|---|
| Apify/Apollo | Paid/external service. Requires `APIFY_TOKEN`; can incur cost. |
| NATO DIANA live search | External search/pages plus Claude extraction. Not ideal live unless prepared. |
| Live enrichment/dossiers | External page fetching plus Claude calls. Requires API key and time budget. |

Do not run live external collectors or paid enrichment during a demo unless the Preflight panel is green, API keys are present, and API/cost exposure is acceptable.

`src/collectors/enrich_leads.py` is not part of the demo path. Treat it as experimental placeholder code; use `src/enrich.py` for actual enrichment.

---

## Clean Demo Runbook

1. Run the migration/preflight DB init:

```bash
./venv/bin/python -m src.db.migrate --db data/companies.db --verbose
```

2. Optionally inspect promotion without changing company/link/promotion rows:

```bash
./venv/bin/python -m src.collectors.promote --dry-run --limit 25
```

3. Promote raw/canonical leads into the dashboard company table:

```bash
./venv/bin/python -m src.collectors.promote
```

4. If `ANTHROPIC_API_KEY` is present and live API use is acceptable, run a small enrichment batch:

```bash
./venv/bin/python src/enrich.py --limit 10
```

5. Run scoring and classification:

```bash
./venv/bin/python src/score.py
./venv/bin/python src/classify.py
```

6. Launch the dashboard:

```bash
./venv/bin/streamlit run src/dashboard.py
```

7. Demo flow:

```text
Sidebar Preflight -> Deal Flow -> enable Show unenriched companies if needed
-> Press -> Portfolio -> Company detail page
```

---

## Useful Safe Checks

Promotion smoke test using only a temporary `/tmp` DB:

```bash
./venv/bin/python -m src.collectors.promote --smoke-test-temp
```

Syntax checks:

```bash
./venv/bin/python -m py_compile src/dashboard.py src/ui/preflight.py src/collectors/promote.py
```

---

## Troubleshooting

**Missing dependencies**

Run from the repo root with the virtualenv active:

```bash
source venv/bin/activate
pip install -r requirements.txt
```

Then use `./venv/bin/python` and `./venv/bin/streamlit` in commands to avoid accidentally using a system Python.

**Wrong DB path**

The demo DB is `data/companies.db`. If the dashboard appears empty, check the sidebar Preflight DB path. Do not inspect or copy from `src/companies.db`; it is not the active DB.

**Empty dashboard**

Open sidebar `Preflight` and check `companies`, `raw_leads`, and `canonical_companies` counts. If `raw_leads` and `canonical_companies` have rows but `companies` is zero, run:

```bash
./venv/bin/python -m src.collectors.promote --dry-run --limit 25
./venv/bin/python -m src.collectors.promote
```

**Raw leads not showing in Deal Flow**

This is expected until promotion runs. Deal Flow reads `companies`, not `raw_leads`.

**Promoted companies still hidden**

Deal Flow hides companies with no description, enrichment, or dossier unless `Show unenriched companies` is enabled. Toggle it in the sidebar or run enrichment.

**Missing API keys**

Preflight shows API key presence as booleans only. `ANTHROPIC_API_KEY` is required for Claude-backed enrichment/dossiers. `APIFY_TOKEN` is required for Apify/Apollo workflows. Do not paste secret values into logs, docs, or screenshots.

**Apify cost warning**

Apify/Apollo collectors are paid/external workflows. Do not run them live during a demo unless cost caps, token presence, and expected behavior are confirmed.
