#!/usr/bin/env bash
# Shared finalizer for Python changes. `--no-sync` preserves manually
# installed editable dependency checkouts in the project environment.

set -eu
trap 'exit 2' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

FORMAT_PATHS=(src/hvbattle tests)
TYPE_PATHS=(src/hvbattle)

uv run --no-sync black "${FORMAT_PATHS[@]}" >&2
uv run --no-sync ruff check --fix "${FORMAT_PATHS[@]}" >&2
uv run --no-sync black "${FORMAT_PATHS[@]}" >&2
uv run --no-sync mypy "${TYPE_PATHS[@]}" >&2
