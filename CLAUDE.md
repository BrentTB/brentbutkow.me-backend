# CLAUDE.md — brentbutkow.me-backend

Recall Radar: multi-country food-recall aggregation API (FastAPI + SQLAlchemy 2 + Postgres), plus
subscription digest emails, a contact form, and a game leaderboard. Layout, endpoints, and deploy
notes live in README.md. This file is the rules past bugs taught — each cites its source PR/commit.

## Commands (always the project venv)

- Lint/format: `.venv/bin/ruff check .` and `.venv/bin/ruff format --check .`
- Types: `.venv/bin/mypy app scripts`
- Tests: `.venv/bin/pytest -q` (DB-free by default; Postgres integration only with `TEST_DATABASE_URL`)
- Migrations: `.venv/bin/alembic revision --autogenerate -m "..."` / `.venv/bin/alembic upgrade head`

Work is done only when all four pass — CI runs exactly these. mypy is the one most often skipped;
run it even for "trivial" changes (missed twice, surfacing only at commit time).

## Wire contract

- Every JSON response is camelCase via `CamelModel` (app/camel.py). Hand-built dicts and
  `model_dump()` need `by_alias=True` — plain `model_dump()` emits snake_case even on a CamelModel.
- The frontend consumes this API: renaming/removing a response key is a breaking change needing
  cross-repo coordination — say so explicitly, never break it silently (#23→#24 NullspaceCounts).

## Adding a recall country/source — complete checklist

#26 added Canada; #27 had to fix what it missed. Touch ALL of these:

1. `RecallCountry`/`RecallSource` enums — app/modules/recalls/schemas.py
2. `_COUNTRY_SOURCES` — app/modules/recalls/service.py
3. Agency display names — app/subscriptions/email.py `_COUNTRY_SOURCES`/`_ALL_SOURCES`; never
   hardcode agency lists inside templates (#27)
4. Admin per-country counts — app/modules/admin/schemas.py + service.py
5. scripts/ingest_all.py, a new scripts/ingest_<src>.py, and a step in .github/workflows/ingest.yml
   (dispatch must stay the last step — a digest must never run off a partial ingest)
6. Human-readable strings listing countries/agencies — grep the *values* (`"us, uk"`, `"FDA"`),
   they hide in Query descriptions and docstrings outside any diff hunk (#27)
7. The dispatch backfill guard is per-country: seeding a new country's history must not suppress
   other countries' digests (#26)
8. Class prediction (app/modules/recalls/class_predictor.py): a new country WITH a native
   Class I/II/III system joins the training set (train_class_predictor.py `_TRAIN_COUNTRIES`, then
   retrain + re-commit the joblib); one WITHOUT joins `PREDICT_COUNTRIES` so build_predictions fills
   its `predicted_class`. Never predict for a country that has a real `classification`.
9. Tests (fixtures from real feed records) + README

## Derived analytics columns (topic_id, event_cluster_id, predicted_class, novelty_score)

These are materialized offline (build_analytics / build_events / build_predictions), never at
ingest. Every write MUST preserve `recalls.updated_at` (set it to itself in a Core UPDATE) — it's
the "source changed" signal build_stats/build_analytics read for staleness, so a derived write that
bumps it makes every rebuild re-run forever. novelty rides in build_analytics' topic_id UPDATE;
predicted_class in build_predictions'. The models (classifier.joblib, class_predictor.joblib) load
ONLY in offline scripts — never import class_predictor from the request path (constraints.txt pins
the sklearn stack the pickles were built with; re-pin + retrain together).

## External feeds (openfda / fsis / fsa_uk / ncc_za / cfia_ca)

Every feed bug so far shipped to prod and needed a backfill script to repair the corpus
(#16 states, #27 brands, #28 HTML entities). Rules:

- Normalize at ingestion, once: apply `strip_html` / `parse_date` / `parse_us_state`
  (app/modules/recalls/normalize.py) to EVERY text/date/state field a feed provides. When fixing
  one field, sweep all sibling fields from the same feed (#28).
- Map every identifying field the feed offers — CFIA has brand but no company; dropping brand made
  digest lines meaningless (#27). A missing required value raises `ValueError`; never fall back to
  `""` (empty composite-PK collision risk).
- Test fixtures copy REAL feed records verbatim — #26's invented fixture shape hid the brand bug.

## Subscriptions / dispatch (mass-send safety)

The first-run dispatcher emailed a real subscriber 137 backlog recalls (#19→#21). Invariants:

- "First run" and "cursor reset" are normal production states, not edge cases. The newness cursor
  is the persistent `DispatchState` row — never process-local state.
- Every bulk-send path keeps a cap or date-window guard from day one, scoped per-country.
- All return paths of `run_dispatch` (early returns included) return the same summary keys — a
  consumer spreads them (#26 review).
- Email mocks raise the real SDK exception (`resend.exceptions.ResendError`), not httpx ones —
  wrong-exception mocks once made retry and bounce handling dead code while tests passed.

## Database & migrations

- Uniqueness/validity invariants go in at table creation (constraint/index, plus
  `pg_advisory_xact_lock` around select-then-insert) — retrofitting them cost irreversible dedupe
  migrations (#16, #25).
- A constant or SQL expression shared by a model and a migration (search exprs, state codes) gets a
  sync test importing both — "kept in sync" comments drift (#16, #22). Column types must match
  between model and migration (`String(30)` vs `Text` slipped through once).

## Config & security

- All env access goes through `Settings` (app/config.py) — never `os.getenv` at import time; it
  freezes values and breaks test/env overrides.
- Optional string settings need a blank→None `field_validator` — `VAR=` (blank) loads as `""`, and
  an empty CORS regex once matched everything (#2).
- Secrets ride in headers (`Authorization`, `X-Internal-Token`, compared with
  `hmac.compare_digest`), never URL query params. Never persist raw `str(exc)` from HTTP clients —
  exception strings embed the request URL and whatever it carries (46ae6e3).
- External-feed values interpolated into URLs get `urllib.parse.quote` per path segment —
  HTML-escaping does not neutralize `/ ? # @`.

## Tests

- Every behavior change ships a test that FAILS without the change — the single most repeated
  review finding. Test the real function, not a re-implementation of its loop.
- Escape LIKE wildcards (`%`, `_`) in user search terms before `.ilike()`.
- Health-style endpoints accept HEAD as well as GET (uptime monitors probe with HEAD, #17).
