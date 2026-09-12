# Person C live smoke checklist

Use this checklist for the issue #53 Render and Resend handoff. Person A enters and owns the
Render and Resend environment values; Person C verifies the delivery behavior without copying
those values out of the provider dashboards.

## API and digest checks

- [ ] `GET /v1/health/live` returns HTTP 200 with the expected liveness response.
- [ ] `GET /v1/health/ready` returns HTTP 200 only while PostgreSQL is reachable.
- [ ] Verify readiness fails closed when PostgreSQL is unavailable and that its response contains
      no database URL, credential, hostname, exception text, or other secret.
- [ ] `GET /v1/digests` returns the published digest list without exposing draft or review digests.
- [ ] Select one published digest ID from that list and verify `GET /v1/digests/{digest_id}` returns
      its claims and official citations.

## Controlled subscription check

Complete these steps only after Person A has entered the approved Render and Resend environment
values and confirmed that the subscription routes are mounted:

- [ ] Submit one subscription request using the single approved team address.
- [ ] Confirm that the generic subscribe response does not reveal whether the address already
      exists or expose a raw confirmation token.
- [ ] Open the delivered confirmation link and complete confirmation through the browser's POST
      flow.
- [ ] Confirm that no email address, raw token, API key, provider payload, full confirmation URL,
      or database URL appears in application logs or recorded smoke-test evidence.
- [ ] Set `VITE_SUBSCRIPTIONS_ENABLED=true` in Render only after the subscription and confirmation
      smoke test passes; do not commit the flag as enabled in Git.

## Ownership and evidence

- [ ] Record that Person A entered the Render and Resend values; Person C did not receive, copy, or
      enter them.
- [ ] Record the deployed commit SHA, UTC test time, HTTP status for each check, and a pass/fail
      result in issue #53.
- [ ] Record only sanitized results. Do not include account IDs, subscriber addresses, tokens,
      secrets, request headers, provider payloads, database URLs, or raw logs.
- [ ] Leave issue #53 open until Person A completes the provider configuration and the controlled
      subscription smoke test passes.
