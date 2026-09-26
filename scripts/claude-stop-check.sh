#!/usr/bin/env bash
# Stop hook: if Python sources or tests changed and are uncommitted, require lint, types and unit
# tests to pass before Claude ends its turn. Exit 2 = block and show stderr to Claude.
# Claude Code stops re-blocking after several consecutive blocks, so a genuinely stuck session still ends.
set -uo pipefail
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
[ -f pyproject.toml ] || exit 0                      # before P0-01 there is nothing to check
changed=$(git status --porcelain -- src tests 2>/dev/null | grep -E '\.py$' || true)
[ -z "$changed" ] && exit 0

out=$(make check 2>&1)
status=$?
if [ $status -ne 0 ]; then
  {
    echo "make check failed with uncommitted changes in src/ or tests/. Fix it before finishing."
    echo "If you are blocked on a question for the human, say so explicitly and leave the failing"
    echo "test in place; do not weaken or skip tests."
    echo "--- last 60 lines ---"
    echo "$out" | tail -n 60
  } >&2
  exit 2
fi
exit 0
