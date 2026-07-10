---
name: cr
description: >-
  Security-focused code review of all staged changes (committed + staged working-tree) between the
  current branch and main, for this Python/FastAPI backend. Runs the CI quality gates, reads
  surrounding context for every changed region, then reviews — security first (authz, secrets, SQL
  injection, input validation, SSRF, CORS, rate limiting, info leakage), then correctness bugs
  (data-flow tracing, edge cases, idempotency), this repo's documented bug-history invariants,
  incomplete propagation, Python/FastAPI conventions, and refactoring. Every finding must carry
  evidence and a concrete failure scenario. Use when the user asks for a code review, CR, or a
  second pair of eyes on staged backend work before committing or pushing.
model: opus
---

# CR — Security-focused code review against main

Review **staged changes** (committed branch work + `git add`-ed working-tree) between this branch and
`main`. Goal: focused, high-signal — **security above all**, then correctness. Not a lecture on the
whole codebase.

Every feed/dispatch bug this repo has shipped reached production and needed a backfill script or an
incident fix (#16, #19, #27, #28 — see CLAUDE.md). The review's job is to catch the **next** one
while it's still a diff. That means hunting for how the change *fails*, not confirming that it
*looks right*: trace real data through the new code, enumerate the inputs that break it, and verify
every claim against the actual code before reporting it.

> Base branch is `main`. If this repo later adopts a `dev` integration branch, swap `main` → `dev` in §1.

## Execution model — keep the raw diff out of the main context

The expensive part (full diffs + surrounding context + grep output) must **not** flood the main window.

- **Small change** (roughly ≤150 changed lines across ≤3 files): review **inline** — do §2 + §3
  yourself, the context is small enough. Skip the fan-out overhead.
- **Bigger**: **fan out**. Group changed files by coupling (a feature module
  `app/modules/<name>/` — its `router.py` + `service.py` + `models.py` + `schemas.py` + helpers +
  the matching `tests/test_<name>*.py` = one group; a feed source + its ingest/backfill scripts +
  fixtures = one group; standalone files = their own). Spawn **one subagent per group** (Agent tool,
  `general-purpose`, all in parallel — multiple Agent calls in one message).
  Each subagent reads its files' diffs + context + greps **in its own context** and returns **only
  structured findings** — never raw diff or context. Then you synthesize (§4) and report (§5).

  **Connector / hub files** — the changed files everything wires through: `app/main.py` (app factory +
  `include_router`), `app/db.py` (`Base`/engine/`get_session`), `app/config.py` (settings), `app/auth.py`
  (the bearer/admin guards), or any changed file imported across multiple groups — do **not** belong in
  a module group. Leave them for the §4 integration check, where the full set of changes is known.

  Each subagent can't see this file, so **paste §2 + §3 (including the "Verify before reporting"
  rules) into its prompt**, plus: its file paths, the base SHA, and these instructions —
  - read its files' diffs: `git diff <base> -- <paths>` and `git diff --cached -- <paths>`;
  - read surrounding context per §2;
  - grep **all of `app/`** (and `scripts/`, `tests/`, `alembic/`) for every symbol it changed —
    consumers may live in another module or outside the diff;
  - apply every category in §3, **security first**, and verify each finding per the §3 rules;
  - return findings only, each as:
    `severity | security? | title | file:line | failure scenario | evidence | fix`
    — plus a `changed symbols:` list, any stale consumers found, and a short `checked clean:` list
    of the §3 categories it examined and found no issue in (so synthesis can spot coverage gaps).
    No prose, no raw diff.

## 0. Run the quality gates (cheap oracle — main context)

CI runs exactly these; a failure is a finding before any human-style review starts. Run them first
so failures can steer where you look (each output is small — errors only):

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy app scripts
.venv/bin/pytest -q
```

- Any failure lands in the report at 🚨 with the tool's output as evidence. mypy is the one most
  often skipped and has twice surfaced errors only at commit time — never skip it.
- These run against the **working tree**, which may include unstaged edits; if unstaged changes
  exist, note in the report that gate results may not exactly reflect the staged snapshot.
- If the diff touches `alembic/` or any `models.py`, also run `.venv/bin/alembic heads` — it must
  print exactly **one** head; two heads means a mis-parented migration that breaks `upgrade head`.

## 1. Establish scope (cheap — runs in the main context)

Only review what the user changed. Unstaged/untracked files are out of scope.

When the user scopes the review themselves ("the latest commit", "just what I staged"), resolve it
literally before reading anything: "latest commit" means **local HEAD** (`git log -1 --oneline`),
not the latest pushed commit — state the SHA you're reviewing. If the user excludes already-reviewed
commits, don't re-review them.

Run as **separate, bare commands** — no `$(...)` capture, no `||`, no redirects — so each matches an
allowlist prefix and runs without a prompt:

```bash
git rev-parse --abbrev-ref HEAD     # current branch
git merge-base origin/main HEAD     # base SHA — if origin/main is missing, run `git merge-base main HEAD`
```

Substitute the printed SHA **literally** (write the real SHA like `a1b2c3d`, never `$BASE`). These
stay cheap — summaries and file lists only, **not** the full diff:

```bash
git log a1b2c3d..HEAD --oneline   # committed branch changes
git diff a1b2c3d --stat           # committed summary
git diff a1b2c3d --name-only      # committed changed files
git diff --cached --stat          # staged summary
git diff --cached --name-only     # staged changed files
```

No committed changes and nothing staged → tell the user there's nothing to review and stop. Use the
file lists + line counts to pick inline vs fan-out and to group files. Full diffs (`git diff <base> --
<paths>`, `git diff --cached -- <paths>`) are pulled by whoever reviews — you inline, or each subagent.

## 2. Read surrounding context

A diff hunk in isolation lies. For every changed region, read enough to understand it:

- **The full function / route handler / class** containing each change, not just the hunk.
- **The module's imports/exports** — what it exposes, what it depends on.
- **Consumers** — grep the changed symbol across `app/` (+ `scripts/`, `tests/`, `alembic/`) to
  catch propagation gaps.
- **Sibling files** in the module — `router.py`, `service.py`, `models.py`, `schemas.py`, `tests/`.
- **Schema/model definitions** — the Pydantic `schemas.py` and SQLAlchemy `models.py` when data shapes,
  columns, or enums are involved, plus `app/config.py` when settings/env are touched.
- **Test fixtures for any changed feed** — fixtures are real feed records verbatim; you'll walk one
  through the new code in §3b. A fixture that is *not* a verbatim real record is itself a finding
  (#26's invented fixture shape hid a real bug).
- **Migration + model together** — when either changes, read both side by side; their column
  types/constraints must match (a `String(30)` vs `Text` mismatch has slipped through before).
- **CLAUDE.md checklists** — if the diff adds a recall country/source, pull up the "Adding a recall
  country/source" checklist; §3c walks it item by item.

Read and grep liberally. Thoroughness over speed.

## 3. Review checklist

Work each category, **security first and hardest**. For every finding cite `file:line` and explain
**why** it's a problem, not just **what**.

### Verify before reporting (applies to every category)

A finding earns its place in the report by surviving an attempt to kill it:

- **Evidence**: read the actual code path (not just the hunk) or run the grep that proves the claim.
  "Searched `app/`, the bearer guard is missing on this route" is a finding; "this might be
  unprotected" is a guess.
- **Failure scenario**: name the concrete input/state and the wrong behavior it produces
  ("a RASFF record with no `subject` field → `KeyError` at ingest, batch aborts"). If you cannot
  construct the scenario, the finding is a 📝 Note at most — or noise, drop it.
- **Clean bills carry the same burden.** A "looks good", a "verified clean" line, or a severity
  downgrade is a claim too, and a wrong one is worse than a missed finding — it actively tells the
  reader not to look. Before crediting a mitigation ("it's cached", "rate-limited", "best-effort,
  one failure can't abort the batch", "the constraint prevents it"), read the mechanism end-to-end
  and confirm it provides the protection you're crediting: a `Cache-Control` response header is not
  a server-side cache (with no CDN it does not shield the origin); a narrow `except` tuple is not
  best-effort isolation. If you didn't verify it, don't assert it — omit it or mark it unverified.

This is not just a precision filter — constructing the failure scenario forces the end-to-end read
that surfaces the *other* bug sitting next to the one you suspected.

### a. Security (review first, cite the impact/exploit)

- **Secrets & config**: no hardcoded tokens, passwords, DB URLs, or API keys in committed code — all
  via `app/config.py` env settings. `.env` must never be committed (only `.env.example`, placeholders).
  A new required setting must also land in `.env.example` + the README deploy notes.
- **Authn / authz**: every mutating or sensitive route (POST/PUT/PATCH/DELETE, ingest, anything
  admin-like) must carry the right guard — `Depends(require_bearer)` for internal/ingest,
  `require_admin` for admin routes (both in `app/auth.py`). Flag any state-changing endpoint
  reachable unauthenticated, and any admin-shaped endpoint carrying only the weaker guard. Token
  comparison stays constant-time (`hmac.compare_digest`) — never `==`.
- **SQL injection**: all DB access through the SQLAlchemy expression API (parameterized). Flag raw SQL
  built with f-strings / `.format` / concatenation into `text()` or `.execute()`. `text()` must use
  bound params (`:name`), never interpolated input.
- **Input validation**: every request body / query / path param is typed and validated via
  Pydantic/FastAPI; pagination bounded (`limit` capped). Untrusted external data (openFDA, RASFF, any
  upstream) is parsed through a Pydantic model before use — never blind `dict` access or `cast`.
- **SSRF / outbound requests**: an httpx/requests call to a URL derived from user input is an SSRF hole.
  Outbound targets must be fixed or allowlisted (feed endpoints are constants) — flag any
  user-controlled host/URL. External-feed values interpolated into URLs get `urllib.parse.quote` per
  path segment — HTML-escaping does not neutralize `/ ? # @`.
- **Mass assignment**: dicts flowing into model construction / `insert().values(**data)` / `setattr`
  must contain only intended, validated fields — never spread raw client input into ORM columns.
- **CORS**: `allow_origins` restricted to known origins from config, not `"*"` — and **never** `"*"`
  together with `allow_credentials=True`.
- **Rate limiting & DoS**: public endpoints are rate-limited (`app/rate_limit.py`); a new public
  endpoint must declare/inherit a limit. Watch unbounded queries (missing `limit`), expensive work,
  or large bodies reachable unauthenticated.
- **Error handling / info leakage**: responses must not leak stack traces, SQL, secrets, or internal
  paths; caught exceptions are logged/stored server-side, not returned verbatim. Never persist raw
  `str(exc)` from HTTP clients — exception strings embed the request URL and whatever it carries.
  No debug/reload in prod.
- **Logging**: no secrets, tokens, full DB URLs, or PII in logs.
- **Transport / DB**: DB connections keep TLS where the provider requires it (e.g. Neon `sslmode=require`).
- **Dependencies**: new/bumped packages in `pyproject.toml` — flag known-vulnerable versions or
  unmaintained/suspicious packages. The pinned sklearn stack (`constraints.txt`) must move together
  with a model retrain, never alone.

### b. Correctness — hunt the bug, don't audit the style

This is where shipped bugs actually live. Don't skim for smells; execute the code in your head
against hostile inputs.

- **Trace the data flow end-to-end.** For a feed change: raw feed record → parser → `normalize.py`
  helpers → normalized dataclass → upsert in `service.py` → API schema → digest email line. At each
  hop ask: what if this field is `None` / missing / empty string / HTML-encoded / a different date
  format / not UTC? A fix applied at one hop with the same flaw at the next hop is the classic
  shape of this repo's shipped bugs.
- **Walk a real fixture record through the changed code.** Take an actual record from the feed's
  test fixtures and simulate the new logic on it line by line. Then do it again with the record's
  optional fields deleted. This catches more than reading ever does.
- **Edge inventory** — check each one that applies: empty feed / zero rows; a batch containing
  duplicate natural keys; unicode + HTML entities (`&amp;`, smart quotes); date boundaries and
  naive-vs-aware datetimes (everything is UTC); off-by-one in ranges/pagination; the first run /
  empty table state (a normal production state here, not an edge case).
- **Idempotency**: re-running an ingest or backfill must not duplicate or clobber rows — check the
  upsert's conflict target matches the real uniqueness invariant. A missing required identifying
  value must raise `ValueError`, never fall back to `""` (empty composite-PK collision).
- **Transactions & partial failure**: commit/rollback paths, cleanup when a batch fails halfway,
  advisory locks (`pg_advisory_xact_lock`) around select-then-insert races.
- **Async correctness**: no blocking I/O (sync httpx, file reads, model loads) inside `async def`
  request handlers; every awaitable awaited.
- **Exception surface**: enumerate what the code inside each `try` can *actually* raise and check
  the `except` tuple covers it. The classic gap: a well-formed error is caught but a
  malformed-but-2xx payload is not — blind `.get()` chains on an untrusted response raise
  `AttributeError`/`TypeError` when the shape is wrong (a list where a dict was expected), not the
  `KeyError`/`ValueError` the author anticipated, and one uncaught shape error can abort a whole
  batch that claims to be best-effort. And the test mocks raise what the *real* library raises
  (`resend.exceptions.ResendError`, not httpx's; wrong-exception mocks once made retry and bounce
  handling dead code while tests passed).
- **Search/query building**: user search terms have LIKE wildcards (`%`, `_`) escaped before
  `.ilike()`; queries on hot paths are bounded.

### c. Repo bug-history invariants (verify actively, don't assume)

Each of these encodes a real shipped bug (CLAUDE.md has the sources). When the diff touches the
area, *check* the invariant — don't trust that the author knew it:

- **camelCase wire contract**: any hand-built dict or `model_dump()` in a response/serialization
  path needs `by_alias=True` — plain `model_dump()` emits snake_case even on a `CamelModel`. Grep
  the diff for `model_dump` and hand-built response dicts. Renaming/removing a response key is a
  breaking frontend change — must be called out explicitly, never silent.
- **Normalize at ingestion, once, and sweep siblings**: every text/date/state field a feed provides
  goes through `strip_html` / `parse_iso_date` / `parse_us_state` / `parse_class`
  (app/modules/recalls/normalize.py). When the diff fixes or adds normalization on one field, check
  every sibling field from the same feed for the same flaw (#28). Every identifying field the feed
  offers must be mapped (#27 — CFIA's dropped brand made digest lines meaningless).
- **Derived analytics never bump `updated_at`**: `topic_id` / `event_cluster_id` /
  `predicted_class` / `novelty_score` are materialized offline; any Core UPDATE to `recalls` from a
  build script must set `updated_at` to itself — bumping it makes every rebuild re-run forever.
  The joblib models load **only** in offline scripts; flag any import of `class_predictor` or a
  classifier loader reachable from the request path.
- **Class prediction country rules**: a new country with a native Class I/II/III system joins
  `_TRAIN_COUNTRIES` (scripts/train_class_predictor.py, + retrain and re-commit the joblib); one
  without joins `PREDICT_COUNTRIES` (app/modules/recalls/class_predictor.py). Never predict for a
  country that has a real `classification`.
- **Dispatch / mass-send safety**: the newness cursor is the persistent `DispatchState` row — never
  process-local. Every bulk-send path keeps a cap or date-window guard, **scoped per-country**
  (seeding one country's history must not suppress or flood other countries' digests). All return
  paths of `run_dispatch` — early returns included — return the same summary keys (a consumer
  spreads them). In `.github/workflows/ingest.yml`, dispatch stays the **last** step.
- **Config discipline**: all env access via `Settings` (app/config.py) — flag `os.getenv` at import
  time. Optional string settings need a blank→None `field_validator` (`VAR=` loads as `""`; an
  empty CORS regex once matched everything).
- **New country/source = walk the CLAUDE.md checklist**: enums, `_COUNTRY_SOURCES` (service *and*
  email), admin counts, scripts + workflow step, class-predictor membership, per-country dispatch
  guard, README/tests — **and grep the human-readable values** (`"us, uk"`, `"FDA"`) hiding in
  Query descriptions, docstrings, and email templates outside any diff hunk. Report each missing
  item as its own finding.
- **HEAD support**: health-style endpoints accept HEAD as well as GET (uptime monitors probe HEAD).

### d. Incomplete propagation

Changes in one place that should have rippled elsewhere — confirm each with a grep across `app/`
(+ `scripts/`, `tests/`, `alembic/`):

- **Renames / signature changes**: a function, Pydantic field, SQLAlchemy column, enum member, or
  constant reshaped at its definition but a caller, test, script, or sibling module still uses the
  old name/shape.
- **Route registration**: an endpoint added/changed in a module's `router.py` but not wired in
  `app/main.py` (`include_router`), or a prefix/path/method mismatch, or `response_model` not updated
  to match the returned shape.
- **Schema ↔ model ↔ migration**: a field added/removed on a SQLAlchemy model but the matching
  Pydantic schema (or vice-versa) not updated; a model change with **no Alembic migration**, or a
  migration whose column types/constraints/server defaults don't match the model. Uniqueness and
  validity invariants belong in the migration that creates the table — retrofitting them has cost
  irreversible dedupe migrations. A constant or SQL expression shared by a model and a migration
  (search expressions, state codes) needs a sync test importing both — "kept in sync" comments drift.
- **Enum / discriminator values**: a new `StrEnum` member added but keyword rules, labels, DB-stored
  values, or the frontend contract not updated (the value-grep from §3c applies here too).
- **camelCase contract**: a new schema field — does it serialize correctly through the alias
  generator (snake_case ↔ camelCase), and does the frontend expect that key?
- **Config / env**: a new `config.py` setting not reflected in `.env.example`, the README, the
  Dockerfile, or the GitHub Actions workflows (ingest cron included).

### e. Code quality

Apply the repo's documented bar (README / CLAUDE.md):

- **Type safety**: full type hints; no unjustified `Any`; validate-don't-cast untrusted data
  (Pydantic); stays mypy-clean (§0 already proved it or flagged it).
- **No magic-string unions**: enum-like sets / discriminators are a `StrEnum` (or a const), not bare
  string literals scattered around.
- **Resource hygiene**: DB sessions closed (the `get_session` dependency, or explicit `close()` in
  scripts), httpx clients/responses closed, no leaked connections/files.
- **Tests**: every new route/function/module ships pytest coverage; **every behavior change ships a
  test that fails without the change** — the single most repeated review finding. The test must
  exercise the real function, not a re-implementation of its loop. Feed fixtures are real records
  verbatim. A new endpoint or changed behavior without a test is a must-fix.
- **Conventions**: snake_case Python, camelCase JSON at the API edge via Pydantic aliases;
  feature-per-folder under `app/modules/`; ruff-clean (no bare `except`, specific exceptions, f-strings).
- **Comments**: lean, present-tense, explain _why_ not _what_. No "previously…/no longer…" narration,
  no restating the code.

### f. Refactoring opportunities

Concrete improvements, not hypothetical future-proofing:

- **Duplication**: two+ blocks doing near-identical work that could share a service function or util
  (repeated query-building, repeated validation/normalization). Only flag genuinely-the-same idea.
- **Simplification**: over-complicated expressions, needless indirection, deep nesting better as early
  returns / guard clauses.
- **Dead code**: new functions, routes, schemas, columns, or settings nothing references. Grep `app/` to
  confirm — zero hits outside the definition means dead.
- **Extraction**: logic sitting in a route handler that belongs in `service.py` or a util, especially
  when it mixes I/O with business logic.

For each suggestion note the rough cost (call sites, code moved) so the user can judge if it's worth it now.

## 4. Synthesize (main context)

When fanning out, collect all subagents' findings, then:

- **Dedupe** overlapping findings (same `file:line` + issue).
- **Coverage check**: union the subagents' `checked clean` lists against §3's categories. A group
  that touched a feed but reported nothing under §3b/§3c wasn't reviewed hard — send one follow-up
  subagent at the gap rather than assuming clean.
- **Reconcile propagation across groups**: union every subagent's `changed symbols`; merge cross-group
  consumer gaps into single findings (each subagent already grepped all of `app/`).
- **Integration check at connector files**: for each hub file set aside in the Execution model section
  (plus any file the unioned `changed symbols` show is imported by ≥2 groups), read just that file and
  confirm it correctly wires every module's changes — routers registered in `app/main.py`, settings
  threaded from `app/config.py`, sessions/`Base` used correctly from `app/db.py`, the right guard
  applied. This is the one place a whole-PR view is needed; hub files are small, so do it here. If a
  hub file is large, delegate it to one more subagent with the `changed symbols` list.
- **New-country reconciliation**: if the diff adds a country/source, assemble the CLAUDE.md
  checklist verdict here — every item either confirmed (with where) or reported missing.
- **Group by severity** for the report.

Don't re-read module diffs here — work from the returned findings.

## 5. Report

Lead with **Security** (the focus), then severity buckets. Keep each tight: what, where (`file:line`),
the failure scenario, and the fix.

```
## Code review — <branch> vs main

### Quality gates
<One line per gate: pass, or the failure summary. Note if unstaged edits may skew results.>

### 🔒 Security
<Always present. List every security finding with its severity, or state
"No security issues found in the reviewed changes." This leads the report.>

### 🚨 Critical
- **<title>** — `app/path/file.py:42`
  <1-2 sentences: the failure scenario — what input/state produces what wrong behavior.>
  Fix: <the concrete change.>

### ⚠️ Warnings
- ...

### 📝 Notes
- ...

### ✅ Looks good
<Brief note on what's solid — only what was actually verified. Keep it short.>
```

- **🔒 Security**: always shown first; surface every security finding here (also tag it in its severity
  bucket). If none, say so explicitly — a clean security pass is worth stating.
- **🚨 Critical**: security holes, bugs with a demonstrated failure scenario, broken propagation,
  missing migrations, missing tests, unguarded mutating endpoints, failing quality gates.
- **⚠️ Warnings**: code-quality issues, convention violations, weak validation, invariant checks
  that couldn't be fully confirmed.
- **📝 Notes**: refactoring, simplification, minor style, findings without a constructible failure
  scenario.

Empty category → say so in one line and move on. Don't manufacture findings. A clean diff gets a short review.

## 6. Offer to fix

After the report, offer to apply fixes — don't apply unprompted:

> Want me to apply these? I can do the 🔒 security + 🚨 must-fixes (safe, mechanical), or all of them, or
> just specific ones — your call.

When fixing:

- Apply clear-cut, low-risk fixes directly.
- For anything with a judgment call or behavior change, confirm the approach first.
- After editing, re-run all four §0 gates (the project's isolated venv). mypy is not optional — type
  errors have twice surfaced only at commit time after fixes were declared done. Report results.
- Keep fixes scoped to the review — no out-of-scope changes.
