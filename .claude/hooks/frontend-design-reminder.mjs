#!/usr/bin/env node
// PreToolUse backstop: when Claude edits a user-visible UI file, remind it to apply the
// frontend-design skill. Non-blocking (permissionDecision: allow) — the edit still proceeds.
import { readFileSync } from 'node:fs'

let data
try {
  data = JSON.parse(readFileSync(0, 'utf8') || '{}')
} catch {
  process.exit(0)
}

const filePath = data?.tool_input?.file_path ?? ''

// User-visible markup/styles only. Skip tests, type decls, and configs.
const isUi = /\.(tsx|jsx|scss|css|html)$/.test(filePath)
const isExcluded = /\.(test|spec)\.|\.types\.ts$|\.config\./.test(filePath)

if (isUi && !isExcluded) {
  process.stdout.write(
    JSON.stringify({
      hookSpecificOutput: {
        hookEventName: 'PreToolUse',
        permissionDecision: 'allow',
        additionalContext:
          `This edits a user-visible UI file (${filePath}). Invoke the frontend-design skill and ` +
          `apply its principles (distinctive, production-grade design; avoid generic AI aesthetics) ` +
          `before/while writing the markup and styles. If already applied this turn, just proceed.`,
      },
    })
  )
}

process.exit(0)
