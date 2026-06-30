# Production env template for Render — 1Password secret references (no secret values; safe to commit).
#
# Resolved at container start by `op run --env-file=.env.render.tpl -- …` (see Dockerfile CMD).
# Render provides only the OP_SERVICE_ACCOUNT_TOKEN env var (a read-only Developer-vault service
# account); op then injects everything below into the process environment.
#
# PORT is intentionally OMITTED — Render injects it automatically, and the CMD reads ${PORT}.
# Resolving PORT from 1Password here would override Render's value and break port binding.

DATABASE_URL=op://Developer/Neon-Postgres/CONNECTION_STRING

INGEST_BEARER_TOKEN=op://Developer/API-Backend/INGEST_BEARER_TOKEN
INTERNAL_DISPATCH_TOKEN=op://Developer/API-Backend/INTERNAL_DISPATCH_TOKEN
ALLOWED_ORIGIN=op://Developer/API-Backend/ALLOWED_ORIGIN
ALLOWED_ORIGIN_REGEX=op://Developer/API-Backend/ALLOWED_ORIGIN_REGEX
TRUSTED_PROXY_HOPS=op://Developer/API-Backend/TRUSTED_PROXY_HOPS

RESEND_API_KEY=op://Developer/Resend/API_KEY
RESEND_FROM_ADDRESS=op://Developer/Resend/NOTIFICATION_FROM_ADDRESS
OPERATOR_EMAIL=op://Developer/Resend/OPERATOR_EMAIL
