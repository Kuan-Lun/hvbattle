#!/usr/bin/env bash
# Shared finalizer for Python changes. `--no-sync` preserves manually
# installed editable dependency checkouts in the project environment.

set -eu
trap 'exit 2' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

PY_FILES=()
while IFS= read -r -d '' file; do
    if [[ -f "$file" ]]; then
        PY_FILES+=("$file")
    fi
done < <(
    git ls-files --cached --others --exclude-standard -z -- '*.py' '*.pyi'
)

if [[ ${#PY_FILES[@]} -eq 0 ]]; then
    exit 0
fi

TYPE_PATHS=(src/hvbattle)

uv run --no-sync black "${PY_FILES[@]}" >&2
uv run --no-sync ruff check --fix "${PY_FILES[@]}" >&2
uv run --no-sync black "${PY_FILES[@]}" >&2
uv run --no-sync mypy "${TYPE_PATHS[@]}" >&2
