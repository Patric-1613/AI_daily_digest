#!/usr/bin/env sh
set -eu

# Render's pre-deploy command is unavailable on Free web services. Apply the
# idempotent Alembic history before replacing this process with Uvicorn so the
# service never accepts traffic on an unmigrated schema.
.venv/bin/alembic upgrade head
exec .venv/bin/uvicorn ai_daily_digest.delivery.api.production:create_production_app \
  --factory --host 0.0.0.0 --port "${PORT}"
