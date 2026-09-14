#!/usr/bin/env sh
set -eu

# Render's pre-deploy command is unavailable on Free web services. Apply the
# idempotent Alembic history before replacing this process with Uvicorn so the
# service never accepts traffic on an unmigrated schema.
.venv/bin/alembic upgrade head
# Render automatically supplies a FORWARDED_ALLOW_IPS environment variable to every Python
# service (observed value: "*") and does not publish a stable reverse-proxy CIDR an operator
# could supply instead, so this flag's value is passed through as-is and is no longer part of
# subscription production configuration (delivery/api/config.py's _subscription_production() does
# not read it at all -- issue #136). The subscription rate-limit network identity is instead
# resolved from Cloudflare's CF-Connecting-IP header on a verified Render web-service runtime; see
# ai_daily_digest.delivery.api.client_network.
exec .venv/bin/uvicorn ai_daily_digest.delivery.api.production:create_production_app \
  --factory --host 0.0.0.0 --port "${PORT}" \
  --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-}"
