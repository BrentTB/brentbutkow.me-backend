# brentbutkow.me-backend

[![API status](https://img.shields.io/uptimerobot/status/m803392574-4c73f9eb6ba6700e79132c3e)](https://stats.uptimerobot.com/OrnjJNCLvf)

General-purpose backend for [brentbutkow.me](https://brentbutkow.me). Modular — each feature is a
self-contained package under `app/modules/`. First module: **Recall Radar**, a US + UK food-recall API
that ingests [openFDA](https://open.fda.gov/apis/food/enforcement/) and USDA FSIS (US) plus UK
[FSA](https://data.food.gov.uk/food-alerts/) data, categorises it, and serves it to the site.

**Stack:** FastAPI · SQLAlchemy 2.0 + Postgres · Pydantic v2 · pytest + ruff. Python is snake_case
throughout; the API emits **camelCase JSON** via Pydantic aliases.

## Layout

```
app/
  config.py        env settings (pydantic-settings)
  db.py            engine + session dependency + Base
  auth.py          bearer dependency
  main.py          FastAPI app, CORS, rate limit, create_all on boot
  modules/recalls/ schemas · models · openfda · fsis · fsa_uk (fetch+normalize+validate) · categorize · classifier (+ model/) · entities · severity · anomalies · analytics (TF-IDF themes + similarity) · service · router
  modules/contact/ schemas · models · service · router — visitor messages (rate-limited, bot-flagged)
  modules/nullspace/ schemas · models · service · router — Null Space game leaderboard (rate-limited, server-side score plausibility checks)
scripts/           per-source ingest + ingest_all · backfill + backfill_all (detects what's needed) · reclassify · classifier training
tests/             categorize · openfda · routes · contact (TestClient, no DB) · service (Postgres integration)
```

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | liveness (no DB hit) |
| GET | `/recalls?limit&offset&country&category&classification&source&state&company&entity&severity&minSeverity&topic&event&since&until&search&sort` | paginated list → `{ items, total }`; `sort` ∈ `recency` (default) · `severity` |
| GET | `/recalls/stats?country` | `{ total, byCategory, byMonth, byClassification, bySeverity, byState, byCompany, bySource, byEntity, anomalies, forecast, lastIngestAt }` |
| GET | `/recalls/trend?country&group&category&classification&source&state&company&entity&severity&minSeverity&topic&event&since&until&search` | monthly counts, optionally grouped by `category` · `source` · `severity` · `classification` → `{ group, buckets }` |
| GET | `/recalls/companies?country&q` | distinct company names matching `q`, ranked by recall count → `string[]` (feeds the filter type-ahead) |
| GET | `/recalls/topics?country` | per-country themes (NMF over reason/product text), largest first → `TopicOut[]` |
| GET | `/recalls/{source}/{recallNumber}/similar?limit` | recalls most similar by reason/product text (precomputed cosine neighbours) → `SimilarRecall[]` |
| GET | `/recalls/events?country&outbreaksOnly` | recall clusters — recalls grouped into one incident (shared pathogen within a time window, or same FDA event); outbreaks first → `EventOut[]` |
| POST | `/recalls/ingest/fda` | **bearer-only** — fetches openFDA, upserts, records an ingest run |
| POST | `/recalls/ingest/fsis` | **bearer-only** — fetches USDA FSIS, upserts, records an ingest run |
| POST | `/recalls/ingest/uk` | **bearer-only** — fetches UK FSA, upserts, records an ingest run |
| POST | `/contact` | **public**, 5/min per IP — stores a visitor message; honeypot + time-trap flag bots as `isBot` |
| GET | `/contact` | **bearer-only** — stored messages, newest first |
| POST | `/nullspace/score` | **public**, 10/min per IP — submit a game score; implausible runs are accepted but hidden from the board |
| GET | `/nullspace/leaderboard?version&limit` | **public** — top scores, highest first; optional `version` scope, `limit` 1–200 |

`category` ∈ `allergen · pathogen · foreignMaterial · mislabeling · contaminant · other`.
`classification` ∈ `Class I · Class II · Class III · Public Health Alert` (US) · `Product Recall ·
Allergy Alert · Food Alert for Action` (UK). `country` ∈ `us · uk`; `source` ∈ `fda · usda · uk`.
`state` matches any affected state; `search` is Postgres full-text over product/reason/company;
`entity` filters to recalls naming a specific allergen/pathogen/hazard/contaminant by its exact
canonical value (e.g. `Listeria`, `peanuts` — the values returned in `byEntity`). Each recall also
carries a `severityScore` (0–100) and `severityLabel` ∈ `low · moderate · high · severe · critical` — a
transparent composite of classification, cause, the named-hazard tier (a lethal pathogen or a
high-risk allergen weighs more than a low-risk one), reported harm in the recall text, and US
geographic breadth, on one scale that puts US classes and UK alert types side by side and lets both
countries span the full range (see `app/modules/recalls/severity.py`). Content modifiers lift the
score with diminishing returns, so heavy profiles spread out near the top instead of saturating at
100 — it rates the **assessed hazard**, not the realised outcome (recall notices carry no reliable
casualty counts). `severity` filters to one band, `minSeverity` to recalls at or above a score,
`sort=severity` orders by it (ties broken toward larger outbreaks, then recency), and `bySeverity`
breaks the corpus down by band. `topic` scopes to a theme by its **stable slug** (e.g.
`listeria-deli-meat`, from `/recalls/topics`) so a bookmarked theme survives an analytics rebuild,
where the surrogate id would not; each recall also carries its `topicId`. `/recalls/topics?country`
lists that country's themes (NMF runs per country) and `/recalls/{source}/{recallNumber}/similar` returns a recall's nearest neighbours
— both materialised offline by `scripts/build_analytics.py` from one shared TF-IDF matrix (NMF themes
+ cosine similarity; see `app/modules/recalls/analytics.py`). `event` likewise scopes to an
**event/outbreak cluster** by its stable slug and each recall carries its `eventClusterId`;
`/recalls/events?country` lists a country's clusters — recalls grouped into one incident by a shared
pathogen within a time window or the same FDA event, with the multi-recall pathogen-driven ones
flagged as outbreaks — materialised by `scripts/build_events.py` (connected components over the
similarity graph; see `app/modules/recalls/events.py`). `stats.anomalies` flags months that
*already* broke from their recent baseline (robust z-score, **detect never predict**); `stats.forecast`
looks the other way — a short-horizon projection of overall monthly volume with a typical-error band,
from a self-built multiplicative seasonal model (a 12-month seasonal index + linear trend fit in log
space, pure numpy; empty when history is too short). `scripts/anomaly_methodology.py` and
`scripts/forecast_methodology.py` validate both offline against statsmodels (STL · Holt-Winters) under
the `ml` extra. The whole `/recalls/stats` payload (these aggregations + anomalies + forecast) is
**materialised per country** by `scripts/build_stats.py` after each ingest — the request path reads
one row instead of recomputing it, with a live fallback when a row is absent. Public reads are
rate-limited (60/min per IP); `POST /contact` is limited to 5/min and `POST /nullspace/score` to
10/min per IP. Interactive docs at `/docs`.

## Local development

Two ways to run it locally — an isolated virtualenv, or Docker Compose (app + Postgres).

### Virtualenv (isolated)

This project runs in its **own `.venv`**, separate from any global/other Python environments. Always
install and run through that venv — never your system `pip`/`python`.

```bash
python3 -m venv .venv
source .venv/bin/activate          # or prefix commands with .venv/bin/
pip install -e ".[dev]"

op inject -i .env.tpl -o .env --force   # generate .env from 1Password (see "Secrets" below)

# DATABASE_URL points at a local Postgres by default; create that database, then:
alembic upgrade head                         # create / update tables (migrations)
uvicorn app.main:app --reload --port 3000   # http://localhost:3000/health  +  /docs
python -m scripts.ingest_fda                 # pull the latest openFDA recalls into the DB
python -m scripts.ingest_fsis                # pull USDA FSIS recalls + alerts (via curl_cffi)
python -m scripts.ingest_uk                  # pull UK FSA food alerts (via curl_cffi)
python -m scripts.ingest_ncc                 # pull South Africa NCC recall notices (via curl_cffi)
python -m scripts.ingest_seed                # upsert the curated SA seed recalls (Woolworths/Shoprite/NRCS)
python -m scripts.ingest_all                 # run all source ingests, then rebuild analytics + events + stats
python -m scripts.backfill_fda               # one-time: seed full openFDA history (~26k records)
python -m scripts.backfill_severity          # one-time: seed severity over existing recalls (after migrating)
python -m scripts.backfill_entities          # one-time: seed entities over existing recalls (after migrating)
python -m scripts.backfill_all               # run the still-needed backfills above (--all forces · --check previews)
python -m scripts.train_classifier           # train the category model → recalls/model/classifier.joblib
python -m scripts.reclassify                 # re-run model + entities + severity over stored recalls
python -m scripts.build_analytics            # rebuild themes + similar-recall neighbours (after ingest)
python -m scripts.build_stats                # re-materialise the /recalls/stats payload per country

pytest                              # tests (no DB needed)
ruff check . && ruff format .       # lint + format
mypy app scripts                    # typecheck
git config core.hooksPath .githooks # one-time: gate commits on ruff + mypy + pytest (auto-formats & re-stages)
```

Schema is managed by **Alembic**: `alembic upgrade head` creates/updates the tables. After changing a
model, generate a migration with `alembic revision --autogenerate -m "describe change"`, review it, and
commit it. (Docker and deploys run `alembic upgrade head` automatically on start.)

**Running scripts against the live DB:** trigger **Actions → Run script on live DB** in GitHub
(manual only — pick the `scripts.*` module from the dropdown) so you don't have to run them from your
laptop. It reads the live Neon connection string from 1Password (via the `OP_SERVICE_ACCOUNT_TOKEN`
repo secret — see [Secrets](#secrets--managed-in-1password)); the daily automated ingest stays in the
separate `Daily recall ingest` workflow.

> **USDA FSIS ingest:** FSIS sits behind Akamai, which 403s plain Python HTTP clients by TLS
> fingerprint. The ingest uses `curl_cffi` (browser-impersonating TLS), so it works from any host —
> the deploy and the daily job included. No proxy needed.

### Docker (app + Postgres)

Runs the whole stack in one command — reads config from your `.env`, but points the app at a bundled
Postgres (`DATABASE_URL` is overridden to the internal `db` service), so it's self-contained:

```bash
op inject -i .env.tpl -o .env --force   # generate .env from 1Password (first time + after vault changes)
docker compose up --build   # http://localhost:3000/health  +  /docs
docker compose exec api python -m scripts.ingest_all # latest recalls (all sources)
docker compose exec api python -m scripts.backfill_fda # one-time: full history (~26k)
docker compose down         # stop (add -v to also wipe the DB)
```

The Compose file uses the project name `website-backend`, so the stack is consistently addressed as `website-backend-api-1` and `website-backend-db-1`.

## Secrets — managed in 1Password

The **1Password `Developer` vault is the single source of truth** for every secret, across all three
places they're consumed. You never hand-edit a local `.env` or set env vars in the Render dashboard —
edit the value in 1Password once, and each consumer picks it up:

| Where | How it resolves | What it needs |
|---|---|---|
| **Local dev** | `op inject -i .env.tpl -o .env --force` regenerates the gitignored `.env` from [`.env.tpl`](.env.tpl) | 1Password CLI signed in (desktop-app integration) |
| **GitHub Actions** | [`1password/load-secrets-action`](https://github.com/1Password/load-secrets-action) resolves `op://` refs per job | `OP_SERVICE_ACCOUNT_TOKEN` repo secret |
| **Render** | `op run --env-file=.env.render.tpl` resolves refs at container start (see [`Dockerfile`](Dockerfile)) | `OP_SERVICE_ACCOUNT_TOKEN` env var |

**One-time setup:**

- **Local:** `brew install 1password-cli`, then enable *Settings → Developer → "Integrate with 1Password
  CLI"* in the desktop app. Re-run the `op inject` command above whenever a vault value changes.
- **CI + Render:** create a 1Password **service account** scoped read-only to the `Developer` vault, then
  set its token as the `OP_SERVICE_ACCOUNT_TOKEN` GitHub repo secret **and** Render env var. Render's
  `PORT` stays auto-injected — it's intentionally not in `.env.render.tpl`.

The templates ([`.env.tpl`](.env.tpl) local, [`.env.render.tpl`](.env.render.tpl) production) hold only
`op://Developer/...` references — no secret values — so they're committed. `.env.example` documents
each variable; the table below is the canonical list.

## Environment

| Var | Required | Notes |
|---|---|---|
| `DATABASE_URL` | ✅ | Postgres connection string. `postgresql://` or `postgres://` both work — the app normalizes to the psycopg driver. |
| `INGEST_BEARER_TOKEN` | ✅ | Guards the `POST /recalls/ingest/*` endpoints. Generate with `openssl rand -hex 32`. |
| `ALLOWED_ORIGIN` | – | CORS origin(s), comma-separated. Defaults to `http://localhost:5173`. |
| `ALLOWED_ORIGIN_REGEX` | – | Optional regex matched in addition to `ALLOWED_ORIGIN`, for origins whose subdomain changes per deploy (e.g. Vercel previews). Anchor it to your own scope — never a blanket `*.vercel.app`. |
| `PORT` | – | Server port. Defaults to `3000`. |
| `TRUSTED_PROXY_HOPS` | – | Reverse-proxy hops in front of the app, so per-IP rate limiting reads the real client from `X-Forwarded-For`. `0` (default) for local/Docker; set `1` behind Render. |

## License

[MIT](LICENSE) © Brent Butkow
