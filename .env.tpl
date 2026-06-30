# Local dev env template — 1Password secret references (the single source of truth).
#
# This file is committed and holds NO secret values, only 1Password secret references. Generate the
# real, gitignored .env from it with:
#
#     op inject -i .env.tpl -o .env --force
#
# (Requires the 1Password CLI signed in — enable Settings → Developer → "Integrate with 1Password
# CLI" in the desktop app.) Re-run after changing any value in 1Password; never hand-edit .env.
#
# Uses the *_LOCAL* field variants where local dev differs from production. docker-compose reads
# the generated .env via env_file and overrides DATABASE_URL to its bundled Postgres.

DATABASE_URL=op://Developer/Neon-Postgres/CONNECTION_STRING_LOCAL

INGEST_BEARER_TOKEN=op://Developer/API-Backend/INGEST_BEARER_TOKEN_LOCAL
INTERNAL_DISPATCH_TOKEN=op://Developer/API-Backend/INTERNAL_DISPATCH_TOKEN
ALLOWED_ORIGIN=op://Developer/API-Backend/ALLOWED_ORIGIN
ALLOWED_ORIGIN_REGEX=op://Developer/API-Backend/ALLOWED_ORIGIN_REGEX_LOCAL
TRUSTED_PROXY_HOPS=op://Developer/API-Backend/TRUSTED_PROXY_HOPS_LOCAL
PORT=op://Developer/Neon-Postgres/PORT

RESEND_API_KEY=op://Developer/Resend/API_KEY
RESEND_FROM_ADDRESS=op://Developer/Resend/NOTIFICATION_FROM_ADDRESS
OPERATOR_EMAIL=op://Developer/Resend/OPERATOR_EMAIL
