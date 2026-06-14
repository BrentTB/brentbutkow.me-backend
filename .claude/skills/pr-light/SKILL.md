---
name: pr-light
description: >-
  Solo-repo fast path to open or update a PR into main for this Python/FastAPI backend, with minimal
  token cost. Skips heavy pre-flight and full-diff reads — pushes if needed, builds a short body from
  commit subjects and file stats (never the raw diff), then creates or updates the PR. Use when you want
  a quick PR and don't need the exhaustive structured description that the `pr` skill produces.
model: sonnet
---

# PR-light — quick PR into main, low token cost

Solo repo. Trust the branch. No exhaustive checks, no reading the full diff.

> Base branch is `main`. If this repo later adopts a `dev` integration branch, swap `main` → `dev` below.

## 1. Push

Read branch name, then push (bare commands, no `$(...)`):

```bash
git rev-parse --abbrev-ref HEAD   # current branch — substitute literally below
git push -u origin my-branch      # push + set upstream; no-op if already up to date
```

If `HEAD` is `main`, stop: "On `main` — check out a feature branch first."

## 2. Read commits + stats (NOT the diff)

```bash
git fetch origin main --quiet
git merge-base origin/main HEAD   # base SHA — substitute literally below
git log a1b2c3d..HEAD --oneline   # commit subjects
git diff a1b2c3d --stat           # files touched
```

Commit subjects + file names are enough — never read the raw diff (that's the token sink).

## 3. Title + short body

Title: imperative, under 70 chars. Body — a few bullets from the commit subjects, grouped if
obvious. Call out any API or DB/migration change. No empty headings, no template scaffolding.

```markdown
## Summary

<!-- 1-2 sentences -->

- ...
- ...
```

Don't claim Claude wrote it.

## 4. Create or update

```bash
gh pr view --json url 2>/dev/null   # exists?
```

Write body to `/tmp/pr-body.md` with the **Write tool**, then:

- **No PR:** `gh pr create --base main --title "<title>" --body-file /tmp/pr-body.md`
- **PR exists:** `gh pr edit --body-file /tmp/pr-body.md`

Print the URL.
