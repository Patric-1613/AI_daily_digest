# Person A live confirmation smoke for issue #53

Use this checklist for the controlled Render and Resend confirmation smoke. Person A owns every
deployed environment value. Person C may help verify behavior but must not receive, copy, or record
those values. This smoke does not close issue #53 because unsubscribe delivery is not shipped.

## 0. Preconditions Person A owns

- [ ] Confirm that the existing Render web service, static site, PostgreSQL database, and approved
      intelligence cron are deployed. Do not create another service or cron for this smoke.
- [ ] In the Render web-service environment—not Git—set `EMAIL_PROVIDER_API_KEY`,
      `EMAIL_FROM_ADDRESS`, `SUBSCRIPTION_TOKEN_ENVIRONMENT`, `SUBSCRIPTION_CONFIRM_KEY_ID`,
      `SUBSCRIPTION_CONFIRM_KEY`, `SUBSCRIPTION_UNSUBSCRIBE_KEY_ID`,
      `SUBSCRIPTION_UNSUBSCRIBE_KEY`, `SUBSCRIPTION_RATE_LIMIT_KEY`, the exact public
      `FRONTEND_ORIGIN`, and `FORWARDED_ALLOW_IPS`. Enter the trusted Render proxy addresses as bare
      IP addresses without CIDR suffixes. Never copy any configured value into an issue, pull
      request, log, or screenshot.
- [ ] Confirm the API starts with the subscription routes mounted. Incomplete or invalid
      configuration must remain fail-closed.
- [ ] Choose one approved team inbox for this smoke. Do not use any other recipient, and do not
      record the address in the test evidence.

## 1. Enable the deployed frontend for the smoke

- [ ] In the Render static-site environment, Person A sets
      `VITE_SUBSCRIPTIONS_ENABLED=true` and rebuilds the deployed frontend before opening a
      confirmation link. The confirmation page is not mounted without this build-time flag.
- [ ] Keep `VITE_SUBSCRIPTIONS_ENABLED` unset or false in Git. This is a Render environment change
      only.
- [ ] If any confirmation step below fails, Person A sets the Render value back to false or removes
      it and rebuilds the static site.

## 2. Run the confirmation path — pass or fail

- [ ] Run the health GET against `GET /v1/health/live` and confirm HTTP 200. This is the deployed
      application's health endpoint.
- [ ] Run the readiness GET against `GET /v1/health/ready` and confirm HTTP 200 while PostgreSQL is
      reachable. Confirm the response contains no database URL, credential, hostname, exception
      text, or other secret.
- [ ] Run `GET /v1/digests` and confirm it returns published digests without exposing draft or
      review digests.
- [ ] Select one published digest ID from that response, then run
      `GET /v1/digests/{digest_id}` and confirm its claims and official citations are returned.
- [ ] Send one JSON `POST /v1/subscriptions` request using the approved team address and the
      required consent field.
- [ ] Confirm the API returns the generic privacy-safe response. The response body must not contain
      the email address, eligibility state, or a raw confirmation token.
- [ ] Confirm one confirmation message arrives in the approved inbox through Resend. Do not copy
      the message body or recipient address into the evidence.
- [ ] Inspect the confirmation link without recording it. Confirm it uses the configured public
      frontend origin and carries the raw token only in the `#token=` URL fragment, never in a query
      string.
- [ ] With the Render frontend flag enabled, open the link on the deployed frontend. Confirm the
      page removes the fragment from the browser address before sending the token in the JSON
      `POST /v1/subscriptions/confirm` request.
- [ ] Complete confirmation and verify the success state. Refresh the page and confirm the raw token
      is not restored to the browser address, displayed by the page, or placed in a request URL.
- [ ] Record only the deployed commit SHA, UTC test time, endpoint status codes, and a pass/fail
      result in issue #53. Do not record an email address, raw token, API key, provider payload,
      request headers, database URL, or the complete confirmation URL with its fragment.

## 3. Record unsubscribe as blocked

No shipped service, repository, or Resend-adapter path sends an unsubscribe token. Therefore the
unsubscribe part of the lifecycle is **BLOCKED** for this smoke.

- [ ] Record unsubscribe as blocked in issue #53 and leave the issue open.
- [ ] Do not manually mint a token, extract one from the database, scrape logs, add a temporary
      endpoint, or otherwise create a new token-delivery path to manufacture a passing result.

## 4. Finish the smoke safely

- [ ] If confirmation passed, Person A may leave `VITE_SUBSCRIPTIONS_ENABLED=true` in the Render
      static-site environment only. Git remains unset or false.
- [ ] If confirmation failed, Person A turns the Render flag off, rebuilds the static site, records
      the sanitized failure stage, and leaves issue #53 open.
- [ ] In either case, leave issue #53 open while unsubscribe remains blocked and keep all secrets,
      recipient data, tokens, provider payloads, and complete token-bearing URLs out of the write-up.
