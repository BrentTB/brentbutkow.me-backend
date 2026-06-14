---
name: pr
description: >-
  Create or update a pull request from the current branch into main, for this Python/FastAPI backend.
  Checks that the branch is pushed and up to date first. Generates a structured PR description from the
  diff (features, API changes, DB/migrations, bug fixes, refactors, tests). Creates the PR if none
  exists, updates an empty description if the PR exists but has no body, or prints the existing
  description if the PR already has one.
model: sonnet
---

# PR — Create or update a pull request into main

> Base branch is `main`. If this repo later adopts a `dev` integration branch, swap `main` → `dev` below.

## 1. Pre-flight: branch ready to PR?

Verify the branch is pushable. Run these checks; **stop with a clear message** if any fail — never
PR from a stale branch.

Run as **separate, bare commands** — no `$(...)` capture, no redirects — so each matches an allowlist
prefix and runs without a prompt. Read the current branch name first, then substitute it **literally**
(write the real name like `my-branch`, never `$BRANCH`):

```bash
git rev-parse --abbrev-ref HEAD                       # current branch (fail if main)
git rev-parse --abbrev-ref --symbolic-full-name @{u}  # remote tracking branch (errors if none)
git fetch origin main --quiet                         # latest remote main
git fetch origin my-branch --quiet                    # latest remote branch
git log origin/my-branch..HEAD --oneline              # unpushed local commits
git log HEAD..origin/main --oneline                   # commits behind main
```

**Stop and tell the user** if:

- Branch **is** `main` — "You're on `main`. Check out a feature branch first."
- **No remote tracking branch** or **unpushed commits** — "You have unpushed commits. Run `git push` first."
- **Behind `origin/main`** (`git log HEAD..origin/main` shows commits) — "Your branch is behind `origin/main`. Rebase or merge main first, then push."

All pass → continue.

## 2. Gather the diff — inline if small, subagent if big

Compute the base and gauge size first (cheap, fine in the main context):

```bash
git merge-base origin/main HEAD   # base SHA — substitute literally below
git diff a1b2c3d --stat           # files + insertions/deletions
```

Read the `--stat` summary line. **Big** ≈ >500 changed lines or >20 files. Else **small**.

**Small — read inline.** The raw diff is small enough for the main context:

```bash
git log a1b2c3d..HEAD --oneline   # commit list
git diff a1b2c3d                  # full diff — read carefully
```

Understand what changed and why (commits + file names + code). Draft the title + body per §3.

**Big — delegate.** A large diff would flood the main context, so spawn **one subagent** (Agent tool,
`general-purpose`) to read it in its own context and return only the distilled result. Prompt it to:

- run `git log a1b2c3d..HEAD --oneline` and `git diff a1b2c3d`, reading the full diff carefully;
- return **only** `TITLE: <title>` on line 1, a blank line, then the markdown body;
- follow the title rule, template, and guidelines from §3 — **paste §3 into the prompt** (the subagent can't see this file).

Parse the `TITLE:` line off the top; the rest is the body. The main context never holds the raw diff.

## 3. Description format

Title: imperative, under 70 chars. Body template — group changes under the applicable headings,
**delete any heading with no entries**:

```markdown
## Summary

<!-- 1-3 sentences: the high-level what and why -->

## Changes

### New features

- ...

### API changes

<!-- new/changed endpoints, request/response shapes, status codes, auth -->

- ...

### Database / migrations

<!-- schema/model changes, new tables/columns, migrations to run -->

- ...

### Bug fixes

- ...

### Refactors

- ...

### Tests

- ...

### Other

- ...
```

Guidelines:

- Specific: "add `POST /recalls/ingest` guarded by a bearer token", not "add endpoint".
- Call out **API contract** and **DB/migration** changes explicitly — reviewers and the frontend depend on them.
- Reference file paths (e.g. `app/modules/recalls/router.py`) where useful.
- One bullet per logical change — group by intent, don't list every file.
- Delete empty sections, don't leave "None".
- Concise but complete — a reviewer should grasp full scope from the description alone.
- NEVER say written by Claude Code — it should read like a human wrote it.

## 4. Create or update the PR

Check whether a PR already exists for this branch into `main`:

```bash
gh pr view --json number,title,body,url 2>/dev/null
```

### Case A — No PR exists

Write the body to a temp file with the **Write tool** (e.g. `/tmp/pr-body.md`), then create with
`--body-file` (no `$(...)`, so no prompt):

```bash
gh pr create --base main --title "<title>" --body-file /tmp/pr-body.md
```

Print the PR URL.

### Case B — PR exists but has an empty body

Write the body to a temp file (**Write tool**, e.g. `/tmp/pr-body.md`), then update:

```bash
gh pr edit --body-file /tmp/pr-body.md
```

Tell the user the PR was updated; print the URL.

### Case C — PR exists and already has a description

Do **not** modify the PR. Print the existing description to the chat, then print the generated one
below it under "**Generated description (not applied):**" so the user can compare or apply it manually.
