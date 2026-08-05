#!/usr/bin/env bash
# Shared finalizer for Markdown changes.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

MD_FILES=()
while IFS= read -r -d '' file; do
    if [[ -f "$file" ]]; then
        MD_FILES+=("$file")
    fi
done < <(
    git ls-files --cached --others --exclude-standard -z -- '*.md'
)

if [[ ${#MD_FILES[@]} -eq 0 ]]; then
    exit 0
fi

PYMARKDOWN_DISABLED_RULES="MD013,MD014"
uv run --no-sync pymarkdown -d "$PYMARKDOWN_DISABLED_RULES" fix \
    "${MD_FILES[@]}" >/dev/null 2>&1 || true

if ! uv run --no-sync ruff format --preview "${MD_FILES[@]}" >&2; then
    exit 2
fi

if ! uv run --no-sync pymarkdown -d "$PYMARKDOWN_DISABLED_RULES" scan \
    "${MD_FILES[@]}" >&2; then
    exit 2
fi
