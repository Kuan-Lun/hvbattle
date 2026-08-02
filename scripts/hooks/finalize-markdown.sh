#!/usr/bin/env bash
# Shared finalizer for Markdown changes.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

MD_FILES=()
while IFS= read -r file; do
    MD_FILES+=("$file")
done < <(
    find . -maxdepth 2 -type f -name "*.md" \
        -not -path "./.venv/*" \
        -not -path "./node_modules/*" \
        -not -path "./.pytest_cache/*" \
        -not -path "./.*" \
        | sort
)

if [ ${#MD_FILES[@]} -eq 0 ]; then
    exit 0
fi

uv run --no-sync pymarkdown fix "${MD_FILES[@]}" >/dev/null 2>&1 || true

if ! uv run --no-sync ruff format --preview "${MD_FILES[@]}" >&2; then
    exit 2
fi
