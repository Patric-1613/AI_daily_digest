#!/usr/bin/env sh
set -eu

# Render's pre-deploy command is unavailable on Free web services. Apply the
# idempotent Alembic history before replacing this process with Uvicorn so the
# service never accepts traffic on an unmigrated schema.
.venv/bin/alembic upgrade head
# Render automatically supplies a FORWARDED_ALLOW_IPS environment variable to every Python
# service (observed value: "*"), independent of any value an operator configures for
# subscriptions. delivery/api/config.py's _subscription_production() deliberately never treats
# FORWARDED_ALLOW_IPS's mere presence as a signal that subscription configuration has started --
# only an application-specific subscription/email setting may do that -- so this default value
# alone can never activate subscriptions or block startup (issue #131).
exec .venv/bin/uvicorn ai_daily_digest.delivery.api.production:create_production_app \
  --factory --host 0.0.0.0 --port "${PORT}" \
  --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-}"
