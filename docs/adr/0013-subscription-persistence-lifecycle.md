# 0013 — Subscription persistence lifecycle and retention

Status: Proposed
Date: 2026-09-10
Issue: [#94](https://github.com/Patric-1613/AI_daily_digest/issues/94)

## Context

ADR 0012 defines the cryptographic token envelope and the security properties of subscription
confirmation and unsubscribe flows. It deliberately leaves two persistence decisions to a later
ADR: the subscription/suppression state machine and the exact retention interval for terminal
token rows. Implementing issue #94 without those decisions would make migration constraints,
concurrent transitions, cleanup, and suppression behavior ambiguous.

This ADR completes that application-owned design. It does not select or configure an email
provider, send email, change Render configuration, or introduce provider credentials. Those
deployment concerns remain outside issue #94.

ADR 0012 originally assigned exact browser routes and rate-limit values to the implementation PR.
This ADR deliberately brings those policy decisions forward so the migration, repository, and
public contract can be reviewed against one unambiguous lifecycle. The later implementation PR
still owns the corresponding `API_CONTRACT.md`, OpenAPI, configuration, and executable-test
changes; this ADR creates no placeholder routes or runtime configuration.

## Decision

### 1. Subscription identity and stored values

Delivery owns one `subscriptions` row per normalized email address. An address is normalized by
trimming surrounding whitespace, applying Unicode NFC normalization, and lowercasing only the
domain part. The local part is preserved because its case semantics are provider-owned. Boundary
validation rejects malformed addresses, control characters, and values longer than 320
characters before persistence.

The normalized address is the only subscriber address stored. It has a database uniqueness
constraint. Responses, logs, metrics, token rows, and delivery rows never contain it. Code that
needs a privacy-preserving rate-limit key uses a purpose-specific keyed HMAC digest; it does not
use a plain unsalted address hash.

Each subscription row contains at least:

- an internal UUID v7 `id`;
- the unique normalized email address;
- `status`;
- `consent_generation`, starting at `1`;
- server-generated `consented_at`, `confirmed_at`, `unsubscribed_at`, and `suppressed_at`
  timestamps where applicable;
- a nullable closed `suppression_reason`; and
- server-generated `created_at` and `updated_at` timestamps.

All timestamps are timezone-aware UTC. Database checks reject impossible state/timestamp
combinations and `consent_generation < 1`.

### 2. State machine

`status` is the closed set `pending`, `confirmed`, `unsubscribed`, and `suppressed`.
`suppression_reason` is null unless status is `suppressed`; its initial closed set is
`hard_bounce`, `abuse_complaint`, and `administrative`.

Allowed transitions are:

| Current state | Event | Result |
|---|---|---|
| no row | eligible subscription request | create generation 1 as `pending` |
| `pending` | repeated eligible request | remain `pending` in the same generation |
| `pending` | valid confirmation token | become `confirmed` in the same generation |
| `confirmed` | repeated subscription request | remain `confirmed`; issue no confirmation token |
| `confirmed` | valid unsubscribe token | become `unsubscribed` in the same generation |
| `unsubscribed` | eligible subscription request | atomically increment generation and become `pending` |
| any state | trusted suppression event | become `suppressed` without changing generation |
| `suppressed` | any public subscription or token request | remain `suppressed` |

No other transition is allowed. Public routes can never clear suppression. Clearing a suppression
requires a future authenticated administrative workflow and is not part of issue #94.
Trusted suppression events cover all three declared reasons: administrative action, hard bounce,
and abuse complaint. Entering `suppressed` revokes every active subscription token and suppresses
unsent delivery work atomically, regardless of which trusted adapter supplied the event. Provider
webhooks and administrative endpoints remain out of scope.

A repeated pending request may create another confirmation token for the same generation, as ADR
0012 permits. It does not increment the generation. A resubscription lifecycle starts only when an
eligible `unsubscribed` row moves back to `pending`; that transition increments the generation,
clears the prior confirmation/unsubscribe timestamps, records a new `consented_at`, and revokes
all active tokens from older generations. Expiry of one confirmation token does not itself create
a new generation.

All subscription-request outcomes return the same response from the API contract. Ineligible,
confirmed, or suppressed addresses do not reveal their state and do not cause a token to appear in
an HTTP response.

### 3. Token persistence and lifecycle

The `subscription_tokens` table follows ADR 0012 exactly. It stores an internal UUID v7 ID,
subscription ID with `ON DELETE RESTRICT`, closed purpose, unique SHA-256 token digest, key ID,
consent generation, creation time, nullable expiry where permitted, first-use time, and revocation
time. It never stores the raw bearer token.

Confirmation tokens expire exactly 24 hours after creation in the MVP. A valid confirmation
transaction locks the token and subscription rows, confirms only the matching pending generation,
records the token's first `used_at`, and revokes unused sibling confirmation tokens for that
generation. A replay cannot repeat the transition and returns ADR 0012's generic invalid-token
error.

An unsubscribe token is issued only for a confirmed generation and has no time-based expiry in the
MVP. Its first valid use atomically marks the subscription unsubscribed, records `used_at`, and
suppresses unsent delivery work. Replaying that same successfully used token returns the same
generic success without another state change. If several delivered messages contain different
unsubscribe tokens for the same current generation, presenting any still-valid sibling after the
subscription is already unsubscribed also returns generic success. The transaction records that
presented token's first `used_at` but does not repeat or reverse the subscription transition and
does not invalidate other current-generation unsubscribe links. A token from an older generation,
a token with the wrong purpose, or a revoked token cannot change subscription state.

Repository operations use one database transaction per state change and row-level locking. Unique
constraints and locked re-reads make concurrent duplicate requests converge on one subscription
row and one valid state transition. No transaction remains open across an email-provider call.

### 4. Retention and cleanup

Terminal token rows are retained for **90 days** before deletion:

- an expired confirmation token becomes eligible 90 days after `expires_at`;
- a used confirmation token becomes eligible 90 days after `used_at`;
- a used unsubscribe token becomes eligible 90 days after `used_at`, unless the
  current-generation unsubscribe rule below keeps it longer;
- a revoked token becomes eligible 90 days after `revoked_at`; and
- when more than one terminal timestamp exists, cleanup uses the latest timestamp.

An active or successfully used unsubscribe-token row for the current confirmed or unsubscribed
consent generation is retained so links from delivered mail and successful retries remain
idempotent. It becomes eligible for the 90-day window only after the generation is superseded or
the token is explicitly revoked. Subscription-level consent, unsubscribe, and suppression audit
state is not deleted by token cleanup.

Cleanup is a scheduled, idempotent database operation that deletes only rows already terminal and
eligible under these rules. Cleanup scheduling is not part of request handling and deletion never
substitutes for first recording expiry, use, or revocation.

### 5. HTTP and browser boundaries

The JSON API uses the three existing contract routes:

- `POST /v1/subscriptions`;
- `POST /v1/subscriptions/confirm`; and
- `POST /v1/subscriptions/unsubscribe`.

Human-facing email links use the configured HTTPS frontend origin and these SPA locations:

- `/subscriptions/confirm#token=<token>`; and
- `/subscriptions/unsubscribe#token=<token>`.

The URL fragment is not sent in the HTTP GET request. Frontend code reads it in memory, immediately
replaces browser history to remove it, renders a minimal page with no third-party scripts, and
requires an explicit user action before making a credentialless JSON POST to the matching API
route. GET requests never mutate subscription state. Token pages send `Cache-Control: no-store`
and `Referrer-Policy: no-referrer`.

RFC 8058 requires a token-bearing HTTPS request target because mailbox providers add only the
standard `List-Unsubscribe=One-Click` form value; they cannot be required to copy a bearer token
from a fragment into a header or body. Application route-template logging alone cannot prevent a
hosting platform from retaining that request target. The RFC 8058 endpoint is therefore disabled
and absent from OpenAPI by default. It must not be deployed or advertised until issue #53 verifies
and documents an end-to-end mechanism that prevents Render, reverse-proxy, application,
observability, and analytics logs from retaining the concrete token-bearing request target.

After that evidence is reviewed, the implementation PR may enable
`POST /v1/subscriptions/unsubscribe/one-click/{token}`. It accepts only
`application/x-www-form-urlencoded` with the exact field
`List-Unsubscribe=One-Click`, requires no browser `Origin`, and follows the same current-generation
idempotency rules as JSON unsubscribe. Enabling it also requires a regression test proving that
application access logs contain only the route template. Provider capability and platform-log
verification remain issue #53 responsibilities.

Browser JSON POST requests require an `Origin` exactly matching the configured frontend origin.
Requests remain credentialless. Malformed, expired, replayed where replay is not allowed,
wrong-generation, and wrong-purpose tokens all fail with ADR 0012's standard privacy-safe error
envelope.

### 6. Rate limiting and privacy-safe observability

Rate-limit state is shared across processes and instances in a Delivery-owned PostgreSQL table.
Each fixed-window counter is identified by `(scope, identity_digest, window_started_at)`, stores an
integer request count and `expires_at`, and has a unique constraint on that identity tuple. A
request consumes capacity with one atomic `INSERT ... ON CONFLICT ... DO UPDATE` operation that
increments and returns the counter. A transaction permits work only when the returned count is at
or below the configured threshold. Counter cleanup is an idempotent deletion of rows whose
`expires_at` is in the past; counters expire 24 hours after their window closes.

The application enforces these independent limits:

- subscription initiation: at most 3 requests per normalized-address HMAC key per hour and 20
  requests per network HMAC key per hour;
- confirmation and unsubscribe token endpoints: at most 30 attempts per network HMAC key per 10
  minutes; and
- if RFC 8058 is later enabled, at most 10 requests per token digest per 10 minutes.

RFC 8058 does not use the 30-per-network browser limit. Mailbox providers legitimately originate
requests for many recipients from shared infrastructure, so a small network ceiling would block
valid unsubscribes. The token is bounded and its SHA-256 digest can be calculated without storing
the raw value; the token-specific counter confines replay to one bearer capability. General
service-level denial-of-service controls remain independent of subscription state and must not
make a valid unsubscribe depend on a shared mailbox-provider network identity.

Address and network identities use separate keyed HMAC purposes. Their keys are separate from
subscription-token signing keys and provided only through secret configuration. The application
never parses or trusts client-supplied `Forwarded` or `X-Forwarded-For` headers. It derives the
client address only from the ASGI connection scope after the server's proxy middleware has accepted
forwarding metadata from an explicit deployment-controlled trusted-proxy allowlist. With no valid
trusted-proxy configuration, the connection peer is used and forwarded headers are ignored;
production must not use a wildcard trusted-proxy setting. The network identity masks validated IP
addresses to IPv4 `/24` or IPv6 `/56` before applying the purpose-specific HMAC.

Raw addresses, network values, request bodies, token values, query strings, full URLs, secrets,
and database URLs are never logged or stored in limit records. Exceeding a limit returns the
standard error envelope with HTTP 429 and a generic message that does not disclose subscription
state.

Logs may include request ID, route template, a coarse outcome category, and an anonymized internal
subscription identifier. Tests use deterministic clocks, token sources, and rate-limit fakes and
make no real provider or network calls.

## Consequences

- Migration constraints and repository transitions can now be implemented without inventing
  lifecycle or retention policy.
- Double opt-in, unsubscribe replay, resubscription generations, and administrative suppression
  have deterministic and concurrency-safe behavior.
- Ninety-day terminal-token retention provides a bounded audit window while current-generation
  unsubscribe links continue to work.
- Fragment-based browser links avoid sending raw tokens in GET requests, but require a small
  frontend handoff page.
- RFC 8058 remains unavailable until the deployed request path is proven absent from every logging
  layer. This preserves ADR 0012's no-raw-token logging guarantee at the cost of delaying one-click
  support.
- Cross-process rate limiting adds a Delivery-owned PostgreSQL table and cleanup operation; an
  in-process-only limiter is insufficient for a multi-instance deployment.
- Provider delivery, Render configuration, real email, authenticated suppression administration,
  and subscriber-data deletion policy remain separate decisions and work items.

## Implementation sequence

After approval by another module owner, issue #94 may add the Delivery-owned migration, models,
transactional repository, service, routes, OpenAPI and API-contract coverage, and frontend flow.
The implementation must retain ADR 0012's purpose-bound token codec and must not expose raw tokens
because an email provider is absent.
