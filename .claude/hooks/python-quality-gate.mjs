#!/usr/bin/env node
// Stop hook: when the working tree has modified Python files, run ruff (lint + format check) and
// mypy; block Claude from finishing until they pass. These are the same checks CI and the
// pre-commit hook run — this just moves the failure before "done" is declared instead of at
// commit time.
import { readFileSync } from 'node:fs'
import { existsSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { join } from 'node:path'

let data
try {
  data = JSON.parse(readFileSync(0, 'utf8') || '{}')
} catch {
  process.exit(0)
}

// Already blocked once this stop cycle — let the stop through to avoid an infinite loop.
if (data?.stop_hook_active) process.exit(0)

const root = process.env.CLAUDE_PROJECT_DIR || process.cwd()
const run = (cmd, args) => spawnSync(cmd, args, { cwd: root, encoding: 'utf8', timeout: 110_000 })

// Only fire when .py files differ from HEAD (modified, staged, or untracked).
const status = run('git', ['status', '--porcelain'])
if (status.status !== 0 || !status.stdout) process.exit(0)
const pyChanged = status.stdout.split('\n').some((line) => /\.py"?$/.test(line.trim()))
if (!pyChanged) process.exit(0)

const ruff = join(root, '.venv/bin/ruff')
const mypy = join(root, '.venv/bin/mypy')
if (!existsSync(ruff) || !existsSync(mypy)) process.exit(0) // no venv — never block on infra

const checks = [
  ['ruff check', ruff, ['check', '.']],
  ['ruff format', ruff, ['format', '--check', '.']],
  ['mypy', mypy, ['app', 'scripts']],
]

const failures = []
for (const [name, cmd, args] of checks) {
  const res = run(cmd, args)
  if (res.error) continue // spawn failure / timeout — skip, don't block
  if (res.status !== 0) {
    const output = `${res.stdout ?? ''}${res.stderr ?? ''}`.trim().slice(0, 3000)
    failures.push(`\`${name}\` failed:\n${output}`)
  }
}

if (failures.length > 0) {
  process.stdout.write(
    JSON.stringify({
      decision: 'block',
      reason:
        `Python quality gate failed — CI runs these same checks, so fix them before finishing ` +
        `(ruff format issues: run \`.venv/bin/ruff format .\`).\n\n${failures.join('\n\n')}`,
    })
  )
}

process.exit(0)
