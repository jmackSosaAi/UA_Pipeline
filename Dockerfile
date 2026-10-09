# Containerised defense-press collector.
#
# Build:
#   docker build -t ua-defense-press .
#
# Run (mount data + logs as volumes so state persists across runs):
#   docker run --rm \
#     -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
#     -v "$(pwd)/data:/app/data" \
#     -v "$(pwd)/logs:/app/logs" \
#     ua-defense-press
#
# Deployment targets — any container scheduler that supports cron-style runs:
#   - Render Cron Jobs   (https://render.com/docs/cronjobs)
#   - Fly.io Machines    (fly.toml + scheduled scale-to-1 then back to 0)
#   - Railway scheduled  (built-in cron)
#   - AWS ECS Scheduled  (with EFS for /app/data persistence)
#   - GCP Cloud Run Jobs (with Cloud Storage / Cloud SQL for persistence)
#
# Persistence note: SQLite at /app/data/companies.db must live on a volume
# that survives the container. For multi-replica deployments, migrate to
# Postgres via DATABASE_URL.

FROM python:3.12-slim

WORKDIR /app

# OS deps — lxml needs libxml2/libxslt headers at install time.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        gcc \
        libxml2-dev \
        libxslt1-dev \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/     src/
COPY config/  config/
COPY scripts/ scripts/

RUN chmod +x scripts/run_defense_press.sh

# data/ holds the SQLite DB; logs/ collects per-day run output.
# Both must be mounted as volumes for persistence across container restarts.
VOLUME ["/app/data", "/app/logs"]

CMD ["bash", "scripts/run_defense_press.sh"]
