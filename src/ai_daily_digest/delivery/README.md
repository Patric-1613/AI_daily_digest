# Delivery module

Owned primarily by Person C. Contains the HTTP API, subscription workflow, email-provider adapter, grounded chat boundary, and frontend-facing services.

The subscription implementation is landing in reviewable slices under issue #94. The token codec
in `subscriptions/tokens.py` implements ADR 0012's bounded, canonical, purpose-specific bearer
envelope. It authenticates token metadata only; expiry, consent generation, revocation and
single-use rules remain database-backed lifecycle checks and must not be inferred from a valid
signature alone. The production factory deliberately leaves all subscription routes unmounted
until issue #53 supplies a confirmation-delivery adapter and an explicitly trusted Render proxy
configuration. Supplying token keys alone does not activate the routes. Render and Resend provider
work is owned separately under issue #53.

It consumes published contracts and must not import private ingestion or intelligence implementations.
