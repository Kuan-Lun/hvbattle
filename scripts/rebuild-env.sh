#!/usr/bin/env bash
# Rebuild a clean development environment whose hvbrowser dependency comes
# from PyPI. Install local editable dependencies manually afterwards when
# cross-repository development is required.

set -euo pipefail
cd "$(dirname "$0")/.."

rm -rf .venv .mypy_cache .ruff_cache .pytest_cache
find . -type d -name __pycache__ -exec rm -rf {} +
uv cache clean --force
uv venv --python 3.14
uv pip install -e ".[dev]"
