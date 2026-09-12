# Person A live subscription smoke for issue #53

Use this checklist for the controlled Render and Resend subscription smoke. Person A owns every
deployed environment value. Person C may help verify behavior but must not receive, copy, or record
those values. This smoke does not close issue #53; closing it remains Person A's deployment
responsibility.

## 1. Complete the Person A preflight

- [ ] Confirm that the existing Render web service, static site, PostgreSQL database, and approved
      intelligence cron are deployed. Do not create another service or cron for this smoke.
- [ ] In the Render web-service environment—not Git—set `EMAIL_PROVIDER_API_KEY`,
      `EMAIL_FROM_ADDRESS`, `SUBSCRIPTION_TOKEN_ENVIRONMENT`, `SUBSCRIPTION_CONFIRM_KEY_ID`,
      `SUBSCRIPTION_CONFIRM_KEY`, `SUBSCRIPTION_UNSUBSCRIBE_KEY_ID`,
      `SUBSCRIPTION_UNSUBSCRIBE_KEY`, `SUBSCRIPTION_RATE_LIMIT_KEY`, the exact public
      `FRONTEND_ORIGIN`, and `FORWARDED_ALLOW_IPS`.
- [ ] Enter trusted Render proxy addresses in `FORWARDED_ALLOW_IPS` as explicit bare IP addresses,
      without CIDR suffixes or `*`. Confirm the API starts with the subscription routes mounted;
      incomplete or invalid subscription configuration must remain fail-closed.
- [ ] Choose one approved team inbox. Do not use another recipient or record the address in the
      smoke evidence.

## 2. Verify health, readiness, and published digests

- [ ] Run the health GET against `GET /v1/health/live` and confirm HTTP 200.
- [ ] Run the readiness GET against `GET /v1/health/ready` and confirm HTTP 200 while PostgreSQL is
      reachable. Confirm the response contains no database URL, credential, hostname, exception
      text, or other secret.
- [ ] Run `GET /v1/digests` and confirm it returns published digests without exposing draft or
      review digests.
- [ ] Select one published digest ID, then run `GET /v1/digests/{digest_id}` and confirm its claims
      and official citations are returned.

## 3. Turn on the deployed frontend subscription pages

- [ ] In the Render static-site environment, Person A sets
      `VITE_SUBSCRIPTIONS_ENABLED=true` and rebuilds the deployed frontend before opening a token
      link. The subscription pages are not mounted without this build-time flag.
- [ ] Keep `VITE_SUBSCRIPTIONS_ENABLED` unset or false in Git. This is a temporary Render
      environment change only.

## 4. Subscribe and confirm — pass or fail

- [ ] Send one JSON `POST /v1/subscriptions` request using the approved team address and required
      consent field.
- [ ] Confirm the API returns the generic privacy-safe response. Its body must not contain the email
      address, eligibility state, or a raw confirmation token.
- [ ] Confirm one confirmation message arrives in the approved inbox through Resend. Do not copy
      the message body or recipient address into the evidence.
- [ ] Inspect the confirmation link without recording it. Confirm it uses the configured public
      frontend origin and carries the raw token only in the `#token=` URL fragment, never in a query
      string.
- [ ] Open the link on the deployed frontend while the Render flag is on. Confirm the page removes
      the fragment from the browser address before sending the token in the JSON
      `POST /v1/subscriptions/confirm` request.
- [ ] Complete confirmation and verify the success state. Refresh the page and confirm the raw token
      is not restored to the browser address, displayed by the page, or placed in a request URL.

## 5. Receive and exercise the unsubscribe link

- [ ] Confirm exactly one separate unsubscribe-link message arrives through Resend after the
      successful confirmation.
- [ ] Inspect the unsubscribe link without recording it. Confirm it uses the configured public
      frontend origin and `/subscriptions/unsubscribe#token=`, never a query string.
- [ ] Open the link on the deployed frontend and complete the explicit JSON
      `POST /v1/subscriptions/unsubscribe` action. Confirm the generic success result.
- [ ] Reopen the same valid link and verify the idempotent success result without another state
      transition or another email.

## 6. Set the deployed frontend flag from the result

- [ ] If either confirmation or unsubscribe delivery/action failed, Person A sets
      `VITE_SUBSCRIPTIONS_ENABLED=false` or removes it from the Render static-site environment and
      rebuilds the site if required for the build-time change to take effect.
- [ ] Leave the flag enabled in Render only after the complete controlled lifecycle passes. Keep it
      unset or false in Git.

## 7. Record sanitized evidence only

- [ ] Record the deployed commit SHA, UTC test time, endpoint status codes, confirmation pass/fail,
      unsubscribe pass/fail, and final frontend-flag state in issue #53.
- [ ] Do not record or copy an email address, raw token, API key, provider payload, request headers,
      database URL, secret, or complete token-bearing URL into the issue, a pull request, a log, or
      a screenshot.
- [ ] Leave issue #53 open.
