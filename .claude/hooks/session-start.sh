#!/bin/bash
# SessionStart hook — provision this project's real toolchain for Claude Code on the web.
#
# Why this exists: the app is Python 3.14 only (`requires-python = "==3.14.*"`), and ruff
# formats to PEP 758 — so `except A, B:` WITHOUT parentheses is correct here. A web
# session's ambient interpreter is older and reports SyntaxError on that perfectly valid
# code. That has already cost one session a bogus "repair" commit against a bug that did
# not exist. Installing the interpreter the project actually targets removes the trap at
# its source rather than relying on anyone remembering the rule.
#
# The ambient `ruff` on PATH is likewise a different version from the pinned one and
# reports findings the pin does not (RUF100 against preview rules). Pre-warm the pin.
#
# Local runs are untouched — a maintainer's own toolchain is already correct.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"

# 1. The interpreter the project targets. ~4 s cold, a no-op once cached. A *bare*
#    interpreter is all we need to parse/compile: `uv run` would try to sync the whole
#    project and fails building psycopg-c (no libpq headers in this image).
uv python install 3.14
PY314="$(uv python find 3.14)"

# 2. The pinned ruff, read from pyproject so this hook cannot drift from the pin.
RUFF_PIN="$(sed -n 's/.*"ruff==\([0-9][0-9.]*\)".*/\1/p' pyproject.toml | head -1)"

if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export RAINRADAR_PYTHON=\"${PY314}\"" >> "$CLAUDE_ENV_FILE"
  [ -n "$RUFF_PIN" ] && echo "export RAINRADAR_RUFF=\"ruff@${RUFF_PIN}\"" >> "$CLAUDE_ENV_FILE"
fi

if [ -n "$RUFF_PIN" ]; then
  uvx "ruff@${RUFF_PIN}" --version >/dev/null   # warm the cache so later calls are instant
fi

cat <<EOF
Toolchain ready:
  Python 3.14 : ${PY314}   (\$RAINRADAR_PYTHON)
  ruff        : uvx ruff@${RUFF_PIN}   (\$RAINRADAR_RUFF)

Ground rules for this repo:
  * Python 3.14 ONLY. Ruff formats to PEP 758, so \`except A, B:\` without parentheses
    is CORRECT. An older interpreter calls that a SyntaxError; it is not one. Parse with
    \$RAINRADAR_PYTHON, never the ambient \`python3\`.
  * Lint with \`uvx \$RAINRADAR_RUFF check .\` — never the \`ruff\` on PATH.
  * The test suite is dockerized and Docker is NOT available here, so a green local
    check is not a green suite. CI is the gate.
EOF
