---
name: cr
description: >-
  Security-focused code review of all staged changes (committed + staged working-tree) between the
  current branch and main, for this Python/FastAPI backend. Reads surrounding context for every changed
  region, then reviews — security first (authz, secrets, SQL injection, input validation, SSRF, CORS,
  rate limiting, info leakage), then correctness, incomplete propagation, Python/FastAPI conventions,
  and refactoring. Use when the user asks for a code review, CR, or a second pair of eyes on staged
  backend work before committing or pushing.
model: opus
---

# CR — Security-focused code review against main

Review **staged changes** (committed branch work + `git add`-ed working-tree) between this branch and
`main`. Goal: focused, high-signal — **security above all**, then correctness. Not a lecture on the
whole codebase.

> Base branch is `main`. If this repo later adopts a `dev` integration branch, swap `main` → `dev` in §1.

## Execution model — keep the raw diff out of the main context

The expensive part (full diffs + surrounding context + grep output) must **not** flood the main window.

- **Small change** (roughly ≤150 changed lines across ≤3 files): review **inline** — do §2 + §3
  yourself, the context is small enough. Skip the fan-out overhead.
- **Bigger**: **fan out**. Group changed files by coupling (a feature module
  `app/modules/<name>/` — its `router.py` + `service.py` + `models.py` + `schemas.py` + helpers +
  the matching `tests/test_<name>*.py` = one group; standalone files = their own). Spawn **one subagent
  per group** (Agent tool, `general-purpose`, all in parallel — multiple Agent calls in one message).
  Each subagent reads its files' diffs + context + greps **in its own context** and returns **only
  structured findings** — never raw diff or context. Then you synthesize (§4) and report (§5).

  **Connector / hub files** — the changed files everything wires through: `app/main.py` (app factory +
  `include_router`), `app/db.py` (`Base`/engine/`get_session`), `app/config.py` (settings), `app/auth.py`
  (the bearer guard), or any changed file imported across multiple groups — do **not** belong in a
  module group. Leave them for the §4 integration check, where the full set of changes is known.

  Each subagent can't see this file, so **paste §2 + §3 into its prompt**, plus: its file paths, the
  base SHA, and these instructions —
  - read its files' diffs: `git diff <base> -- <paths>` and `git diff --cached -- <paths>`;
  - read surrounding context per §2;
  - grep **all of `app/`** (and `scripts/`, `tests/`) for every symbol it changed — consumers may live
    in another module or outside the diff;
  - apply every category in §3, **security first**;
  - return findings only, each as: `severity | security? | title | file:line | why | fix`, plus a
    `changed symbols:` list and any stale consumers found. No prose, no raw diff.

## 1. Establish scope (cheap — runs in the main context)

Only review what the user changed. Unstaged/untracked files are out of scope.

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
- **Consumers** — grep the changed symbol across `app/` (+ `scripts/`, `tests/`) to catch propagation gaps.
- **Sibling files** in the module — `router.py`, `service.py`, `models.py`, `schemas.py`, `tests/`.
- **Schema/model definitions** — the Pydantic `schemas.py` and SQLAlchemy `models.py` when data shapes,
  columns, or enums are involved, plus `app/config.py` when settings/env are touched.

Read and grep liberally. Thoroughness over speed.

## 3. Review checklist

Work each category. For every finding cite `file:line` and explain **why** it's a problem, not just
**what**. **Security is the priority — review it first and hardest.**

### a. Security (review first, cite the impact/exploit)

- **Secrets & config**: no hardcoded tokens, passwords, DB URLs, or API keys in committed code — all
  via `app/config.py` env settings. `.env` must never be committed (only `.env.example`, placeholders).
  A new required setting must also land in `.env.example` + the README deploy notes.
- **Authn / authz**: every mutating or sensitive route (POST/PUT/PATCH/DELETE, ingest, anything
  admin-like) must carry the bearer guard (`dependencies=[Depends(require_bearer)]`). Flag any
  state-changing endpoint that's reachable unauthenticated. Token comparison stays constant-time
  (`hmac.compare_digest`) — never `==`.
- **SQL injection**: all DB access through the SQLAlchemy expression API (parameterized). Flag raw SQL
  built with f-strings / `.format` / concatenation into `text()` or `.execute()`. `text()` must use
  bound params (`:name`), never interpolated input.
- **Input validation**: every request body / query / path param is typed and validated via
  Pydantic/FastAPI; pagination bounded (`limit` capped). Untrusted external data (openFDA, any upstream)
  is parsed through a Pydantic model before use — never blind `dict` access or `cast`.
- **SSRF / outbound requests**: an httpx/requests call to a URL derived from user input is an SSRF hole.
  Outbound targets must be fixed or allowlisted (the openFDA endpoint is constant) — flag any
  user-controlled host/URL.
- **Mass assignment**: dicts flowing into model construction / `insert().values(**data)` / `setattr`
  must contain only intended, validated fields — never spread raw client input into ORM columns.
- **CORS**: `allow_origins` restricted to known origins from config, not `"*"` — and **never** `"*"`
  together with `allow_credentials=True`.
- **Rate limiting & DoS**: public endpoints are rate-limited; a new public endpoint must declare/inherit
  a limit. Watch unbounded queries (missing `limit`), expensive work, or large bodies reachable
  unauthenticated.
- **Error handling / info leakage**: responses must not leak stack traces, SQL, secrets, or internal
  paths; caught exceptions are logged/stored server-side, not returned verbatim. No debug/reload in prod.
- **Logging**: no secrets, tokens, full DB URLs, or PII in logs.
- **Transport / DB**: DB connections keep TLS where the provider requires it (e.g. Neon `sslmode=require`).
- **Dependencies**: new/bumped packages in `pyproject.toml` — flag known-vulnerable versions or
  unmaintained/suspicious packages.

Confirm grep-able claims with a grep before reporting — "searched `app/`, the bearer guard is missing on
this route" is a finding; "this might be unprotected" is a guess.

### b. Incomplete propagation

Changes in one place that should have rippled elsewhere — confirm each with a grep across `app/`:

- **Renames / signature changes**: a function, Pydantic field, SQLAlchemy column, enum member, or
  constant reshaped at its definition but a caller, test, or sibling module still uses the old name/shape.
- **Route registration**: an endpoint added/changed in a module's `router.py` but not wired in
  `app/main.py` (`include_router`), or a prefix/path/method mismatch, or `response_model` not updated to
  match the returned shape.
- **Schema ↔ model ↔ DB**: a field added/removed on a SQLAlchemy model but the matching Pydantic schema
  (or vice-versa) not updated; a column/type change with **no migration** — v1 builds tables via
  `create_all`, so a changed model against an existing DB needs a migration (Alembic is the planned tool).
  Flag the gap.
- **Enum / discriminator values**: a new `StrEnum` member added but the keyword rules, labels,
  DB-stored values, or the frontend contract not updated.
- **camelCase contract**: a new schema field — does it serialize correctly through the alias generator
  (snake_case ↔ camelCase), and does the frontend expect that key?
- **Config / env**: a new `config.py` setting not reflected in `.env.example`, the README, the Dockerfile,
  or the GitHub Actions workflow.

### c. Code quality

Apply the repo's documented bar (README / CLAUDE.md):

- **Correctness & logic**: error/empty/edge handling, transaction correctness (commit/rollback paths,
  partial-failure cleanup), off-by-one, missing/wrong conditionals. For any async code, await/blocking
  correctness.
- **Type safety**: full type hints; no unjustified `Any`; validate-don't-cast untrusted data (Pydantic);
  stays mypy-clean.
- **No magic-string unions**: enum-like sets / discriminators are a `StrEnum` (or a const), not bare
  string literals scattered around.
- **Resource hygiene**: DB sessions closed (the `get_session` dependency, or explicit `close()` in
  scripts), httpx clients/responses closed, no leaked connections/files.
- **Tests**: every new route/function/module ships pytest coverage; **every bug fix ships a regression
  test** that fails without the fix. Routes via `TestClient`, pure logic unit-tested. A new endpoint or
  changed behavior without a test is a must-fix.
- **Conventions**: snake_case Python, camelCase JSON at the API edge via Pydantic aliases;
  feature-per-folder under `app/modules/`; ruff-clean (no bare `except`, specific exceptions, f-strings).
- **Comments**: lean, present-tense, explain _why_ not _what_. No "previously…/no longer…" narration, no
  restating the code.

### d. Refactoring opportunities

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
- **Reconcile propagation across groups**: union every subagent's `changed symbols`; merge cross-group
  consumer gaps into single findings (each subagent already grepped all of `app/`).
- **Integration check at connector files**: for each hub file set aside in the Execution model section
  (plus any file the unioned `changed symbols` show is imported by ≥2 groups), read just that file and
  confirm it correctly wires every module's changes — routers registered in `app/main.py`, settings
  threaded from `app/config.py`, sessions/`Base` used correctly from `app/db.py`, the bearer guard
  applied. This is the one place a whole-PR view is needed; hub files are small, so do it here. If a hub
  file is large, delegate it to one more subagent with the `changed symbols` list.
- **Group by severity** for the report.

Don't re-read module diffs here — work from the returned findings.

## 5. Report

Lead with **Security** (the focus), then severity buckets. Keep each tight: what, where (`file:line`),
why, and the fix.

```
## Code review — <branch> vs main

### 🔒 Security
<Always present. List every security finding with its severity, or state
"No security issues found in the reviewed changes." This leads the report.>

### 🚨 Critical
- **<title>** — `app/path/file.py:42`
  <1-2 sentences: the problem and why it matters.>
  Fix: <the concrete change.>

### ⚠️ Warnings
- ...

### 📝 Notes
- ...

### ✅ Looks good
<Brief note on what's solid — keep it short.>
```

- **🔒 Security**: always shown first; surface every security finding here (also tag it in its severity
  bucket). If none, say so explicitly — a clean security pass is worth stating.
- **🚨 Critical**: security holes, bugs, broken propagation, missing migrations, missing tests, unguarded
  mutating endpoints.
- **⚠️ Warnings**: code-quality issues, convention violations, weak validation.
- **📝 Notes**: refactoring, simplification, minor style.

Empty category → say so in one line and move on. Don't manufacture findings. A clean diff gets a short review.

## 6. Offer to fix

After the report, offer to apply fixes — don't apply unprompted:

> Want me to apply these? I can do the 🔒 security + 🚨 must-fixes (safe, mechanical), or all of them, or
> just specific ones — your call.

When fixing:

- Apply clear-cut, low-risk fixes directly.
- For anything with a judgment call or behavior change, confirm the approach first.
- After editing, re-run `.venv/bin/ruff check .` and `.venv/bin/pytest` (use the project's isolated venv).
  Report results.
- Keep fixes scoped to the review — no out-of-scope changes.
