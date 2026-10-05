#!/usr/bin/env bash
# PostToolUse hook: format and auto-fix the Python file Claude just edited.
# Reads the hook JSON on stdin; never blocks (always exits 0).
set -uo pipefail
file=$(python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("tool_input",{}).get("file_path",""))' 2>/dev/null)
case "$file" in
  *.py)
    cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
    uv run --quiet ruff format --quiet "$file" >/dev/null 2>&1
    uv run --quiet ruff check --fix --quiet "$file" >/dev/null 2>&1
    ;;
esac
exit 0
