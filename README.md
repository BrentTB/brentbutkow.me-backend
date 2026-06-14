# brentbutkow.me-backend

General-purpose backend for [brentbutkow.me](https://brentbutkow.me). Modular — each feature is a
self-contained package under `app/modules/`. First module: **Recall Radar**, a US food-recall API that
ingests [openFDA](https://open.fda.gov/apis/food/enforcement/) data, categorises it, and serves it to
the site.

**Stack:** FastAPI · SQLAlchemy 2.0 + Postgres · Pydantic v2 · pytest + ruff. Python is snake_case
throughout; the API emits **camelCase JSON** via Pydantic aliases.

## Layout

```
app/
  config.py        env settings (pydantic-settings)
  db.py            engine + session dependency + Base
  auth.py          bearer dependency
  main.py          FastAPI app, CORS, rate limit, create_all on boot
  modules/recalls/ schemas · models · openfda (fetch+normalize+validate) · categorize · classifier (+ model/) · service · router
scripts/ingest.py  manual ingest entrypoint
tests/             categorize · openfda · routes (TestClient, no DB)
```

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | liveness (no DB hit) |
| GET | `/recalls?limit&offset&category&classification&state&company&since` | paginated list → `{ items, total }` |
| GET | `/recalls/stats` | `{ total, byCategory, byMonth, byClassification, byState, byCompany, lastIngestAt }` |
| POST | `/recalls/ingest` | **bearer-only** — fetches openFDA, upserts, records an ingest run |

`category` ∈ `allergen · pathogen · foreignMaterial · mislabeling · other`. `classification` ∈
`Class I · Class II · Class III`. Public reads are rate-limited (60/min per IP). FastAPI also serves
interactive API docs at `/docs`.

## Local development

Two ways to run it locally — an isolated virtualenv, or Docker Compose (app + Postgres).

### Virtualenv (isolated)

This project runs in its **own `.venv`**, separate from any global/other Python environments. Always
install and run through that venv — never your system `pip`/`python`.

```bash
python3 -m venv .venv
source .venv/bin/activate          # or prefix commands with .venv/bin/
pip install -e ".[dev]"

cp .env.example .env               # set DATABASE_URL + INGEST_BEARER_TOKEN

# point DATABASE_URL at a local Postgres (any instance), create that database, then:
alembic upgrade head                         # create / update tables (migrations)
uvicorn app.main:app --reload --port 3000   # http://localhost:3000/health  +  /docs
python -m scripts.ingest                     # pull the latest recalls into the DB
python -m scripts.backfill                   # one-time: seed full history (~26k records)
python -m scripts.train_classifier           # train the category model → recalls/model/classifier.joblib
python -m scripts.reclassify                 # re-run the trained model over stored recalls (after training)

pytest                              # tests (no DB needed)
ruff check . && ruff format .       # lint + format
mypy app scripts                    # typecheck
git config core.hooksPath .githooks # one-time: gate commits on ruff + mypy + pytest (auto-formats & re-stages)
```

Schema is managed by **Alembic**: `alembic upgrade head` creates/updates the tables. After changing a
model, generate a migration with `alembic revision --autogenerate -m "describe change"`, review it, and
commit it. (Docker and deploys run `alembic upgrade head` automatically on start.)

### Docker (app + Postgres)

Runs the whole stack in one command — reads config from your `.env`, but points the app at a bundled
Postgres (`DATABASE_URL` is overridden to the internal `db` service), so it's self-contained:

```bash
cp .env.example .env        # first time
docker compose up --build   # http://localhost:3000/health  +  /docs
docker compose exec api python -m scripts.ingest     # latest recalls
docker compose exec api python -m scripts.backfill   # one-time: full history (~26k)
docker compose down         # stop (add -v to also wipe the DB)
```

## Environment

| Var | Required | Notes |
|---|---|---|
| `DATABASE_URL` | ✅ | Postgres connection string. `postgresql://` or `postgres://` both work — the app normalizes to the psycopg driver. |
| `INGEST_BEARER_TOKEN` | ✅ | Guards `POST /recalls/ingest`. Generate with `openssl rand -hex 32`. |
| `ALLOWED_ORIGIN` | – | CORS origin(s), comma-separated. Defaults to `http://localhost:5173`. |
| `ALLOWED_ORIGIN_REGEX` | – | Optional regex matched in addition to `ALLOWED_ORIGIN`, for origins whose subdomain changes per deploy (e.g. Vercel previews). Anchor it to your own scope — never a blanket `*.vercel.app`. |
| `PORT` | – | Server port. Defaults to `3000`. |
| `OPENFDA_API_KEY` | – | Optional; raises openFDA rate limits. |
| `TRUSTED_PROXY_HOPS` | – | Reverse-proxy hops in front of the app, so per-IP rate limiting reads the real client from `X-Forwarded-For`. `0` (default) for local/Docker; set `1` behind Render. |

## License

[MIT](LICENSE) © Brent Butkow
