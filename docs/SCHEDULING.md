# Scheduling the defense-press collector

The collector is wrapped by [`scripts/run_defense_press.sh`](../scripts/run_defense_press.sh)
so the same script works from launchd, a CI runner, or a container — only
the trigger changes. Pick the layer that matches where the database lives.

## Current setup — local launchd (macOS)

The active scheduler is a per-user launchd agent on the operator's Mac.
Database state lives in [`data/companies.db`](../data/) inside the repo.

**Install / reload:**
```bash
bash scripts/install_local_schedule.sh
```

The installer renders [`scripts/launchd/com.uapipeline.defense_press.plist`](../scripts/launchd/com.uapipeline.defense_press.plist)
into `~/Library/LaunchAgents/`, substituting `__PROJECT_ROOT__` with the
absolute repo path, and bootstraps the agent. Default schedule: daily at
**07:00 local time**.

**Inspect:**
```bash
launchctl list | grep com.uapipeline.defense_press      # status
launchctl kickstart -p gui/$(id -u)/com.uapipeline.defense_press   # run now
tail -f logs/launchd_*.log logs/defense_press_*.log   # watch output
```

**Uninstall:**
```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.uapipeline.defense_press.plist
rm ~/Library/LaunchAgents/com.uapipeline.defense_press.plist
```

---

## Manual-run collectors (cost-bearing — do not auto-schedule)

Some collectors call paid third-party APIs and run only on operator
demand. They are intentionally NOT wrapped by `run_*.sh` or scheduled
via launchd/Actions/cron.

### Apify Leads — `src/collectors/apify_leads.py`

Discovers defense companies + senior decision-makers via Apollo's
LinkedIn-keyword search through the
`braveleads/leads-finder-linkedin-apollo-leads-generator` actor.

**Cost:** ~$1.70 per 1,000 leads. Default budget cap $0.85 per run
(≈500 leads); `--no-budget-cap` to override.

**Three-step pattern after every run** — required because the actor
advances its cursor through Apollo's database on every call (a fresh
invocation returns *different* orgs, never the same batch twice):

```bash
# 1. Paid actor call — surfaces new orgs into raw_leads (founders/contacts deferred)
python -m src.collectors.apify_leads --terms "loitering munition,electronic warfare" \
                                      --max-results-per-term 100

# 2. Free — materialise new canonicals into companies
python -m src.collectors.promote

# 3. Free — replay the just-paid dataset to backfill founders/contacts
#    (the actor's runId is in the per-run log file under logs/apify_leads_*.log)
python scripts/_apify_replay.py <RUN_ID>
```

Step 3 (replay) is a **first-class part of the integration**, not a
workaround. Apify keeps run datasets indefinitely and iterating an
existing dataset is free — so once `promote.py` materialises the
new orgs into `companies`, replaying the prior run's dataset writes
founders/contacts at zero marginal cost.

Source-tagging: rows from this collector are tagged
`source_url LIKE 'apify_leads://%'`, which `enrich.py`'s source-aware
DELETE logic preserves across enrichment passes.

---

## Future paths

| Layer | Trigger | DB requirement | When to switch |
|-------|---------|----------------|----------------|
| **Local launchd** | mac wakes & user logged in | in-repo SQLite OK | one-operator phase (now) |
| **GitHub Actions** | `cron:` in [`.github/workflows/defense_press.yml`](../.github/workflows/defense_press.yml) | external DB (Postgres or S3-replicated SQLite) | when daily cadence shouldn't depend on a Mac being awake |
| **Render / Railway cron** | Service "Cron Jobs" feature | mounted volume *or* external Postgres | when a non-engineer should be able to see runs / pause them in a UI |
| **Fly.io machines / AWS ECS Scheduled / Cloud Run Jobs** | container scheduler (cron-spec) | persistent volume + image registry | when the team needs multi-region or HA |

The file [`Dockerfile`](../Dockerfile) is ready for any container target.
The file [`.github/workflows/defense_press.yml`](../.github/workflows/defense_press.yml)
is committed in **dormant** form — `workflow_dispatch` (manual trigger
from the Actions UI) is the only enabled event so the schedule doesn't
fire on top of a fresh ephemeral DB.

---

## Migration triggers

Move off local launchd when **any** of these become true:

1. **The collector misses a day because the Mac was off / asleep.**
   Switch to GitHub Actions (cheapest) once the DB is no longer
   in-repo.
2. **A second operator wants to run / inspect / pause the schedule.**
   Switch to Render Cron or Railway — they expose a UI that doesn't
   require shell access.
3. **A run takes more than ~10 minutes** and starts colliding with
   working hours, *or* you want to fan out to multiple feeds in
   parallel. Switch to a container scheduler (Fly.io, ECS).
4. **Database contention from the Streamlit dashboard** — once the UI
   moves off SQLite, the collector should follow.

The order of these triggers is roughly the order in which they will hit:
expect to do (1) within a few months, (2) only if the team grows.

---

## Testing each layer

### Layer 1 — bash wrapper

```bash
bash scripts/run_defense_press.sh
echo "exit=$?"   # should be 0
tail -n 40 logs/defense_press_$(date +%Y-%m-%d).log
```

### Layer 2 — launchd

After `install_local_schedule.sh`:

```bash
# Smoke test: trigger an immediate run (don't wait for 07:00).
launchctl kickstart -p gui/$(id -u)/com.uapipeline.defense_press
tail -f logs/launchd_*.log
```

### Layer 3 — GitHub Actions

While the schedule is dormant, use the Actions tab → "Defense Press
Collector" → "Run workflow" to dispatch a manual run. Logs upload as
the `defense-press-logs` artifact.

### Layer 4 — Docker

```bash
docker build -t ua-defense-press .
docker run --rm \
    -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
    -v "$(pwd)/data:/app/data" \
    -v "$(pwd)/logs:/app/logs" \
    ua-defense-press
```

The volume mounts ensure the SQLite DB and the day's log survive the
container exit.

---

## What the wrapper actually runs

```text
1. python -m src.collectors.defense_press
   - fetches all active feeds from config/sources/rss_feeds.yaml
   - per article: dedup → keyword filter → Claude extraction → store
   - links every named company to processed_articles via article_companies

2. python -m src.collectors.promote
   - moves new canonical_companies rows into companies
   - allows downstream enrich/score/classify to pick them up
```

Adding `enrich`, `classify`, `score` here later is a one-line change in
[`scripts/run_defense_press.sh`](../scripts/run_defense_press.sh) — they
are intentionally left out for now to keep the nightly fast and avoid
mass-LLM costs on every run.
