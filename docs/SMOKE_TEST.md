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

- [ ] Record the current delivery limitation before starting: no shipped service, repository,
      or Resend-adapter path issues or emails an unsubscribe token. Confirmation can be tested;
      unsubscribe is blocked unless a valid token has already been received through an approved
      application delivery path. Do not invent, manually mint, or extract a token to bypass this
      limitation, and leave issue #53 open while the step is blocked.
- [ ] For this controlled smoke only, Person A may set `VITE_SUBSCRIPTIONS_ENABLED=true` in the
      Render static-site environment and rebuild the deployed frontend. Keep the value unset or
      false in Git. This temporary Render setting is required to mount the browser confirmation and
      unsubscribe pages used below.
- [ ] Submit one subscription request using the single approved team address.
- [ ] Confirm that the generic subscribe response does not reveal whether the address already
      exists or expose a raw confirmation token.
- [ ] Open the delivered confirmation link and complete confirmation through the browser's POST
      flow. Verify the raw token arrived only after `#token=` in the URL fragment, never in a query
      parameter, and that the page removed the fragment before submitting the JSON POST.
- [ ] For the same approved team address, open an unsubscribe link issued by the approved
      application delivery path. Verify it also carries the raw token only after `#token=` and
      complete unsubscribe through the browser's JSON POST flow.
- [ ] Confirm the unsubscribe result is successful. If no approved application path has delivered
      an unsubscribe token yet, record this step as blocked and leave issue #53 open. Do not extract
      a token from the database or logs, manually mint one, add a temporary endpoint, or start
      campaign/RFC 8058 work to make the smoke pass.
- [ ] Confirm that no email address, raw token, API key, provider payload, full confirmation URL,
      or database URL appears in application logs or recorded smoke-test evidence.
- [ ] If the complete subscribe, confirm, and unsubscribe lifecycle cannot pass, Person A turns
      `VITE_SUBSCRIPTIONS_ENABLED` back off in Render and rebuilds the static site. Leave it enabled
      only after the full lifecycle succeeds; never commit the flag as enabled in Git.

## Ownership and evidence

- [ ] Record that Person A entered the Render and Resend values; Person C did not receive, copy, or
      enter them.
- [ ] Record the deployed commit SHA, UTC test time, HTTP status for each check, and a pass/fail
      result in issue #53.
- [ ] Record only sanitized results. Do not include account IDs, subscriber addresses, tokens,
      secrets, request headers, provider payloads, database URLs, or raw logs.
- [ ] Leave issue #53 open until Person A completes the provider configuration and the controlled
      subscription smoke test passes.
