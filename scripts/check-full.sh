#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repository_root"

# ONNX Runtime 1.29 otherwise persists a telemetry session as
# `:memory:.ses` in the current directory while importing the classifier.
export ORT_DISABLE_TELEMETRY=1

scripts/check-fast.sh
.venv/bin/pytest

command -v lake >/dev/null || {
    printf 'check-full: lake is required for the formal safety model\n' >&2
    exit 1
}
(
    cd formal
    lake build
)

command -v uv >/dev/null || {
    printf 'check-full: uv is required for wheel smoke installation\n' >&2
    exit 1
}

smoke_root="$(mktemp -d "${TMPDIR:-/tmp}/hvbattle-wheel-smoke.XXXXXX")"
cleanup() {
    [[ -n "$smoke_root" && -d "$smoke_root" ]] || return 0
    rm -rf -- "$smoke_root"
}
trap cleanup EXIT

.venv/bin/python -m build --no-isolation --outdir "$smoke_root/dist"
wheel="$(find "$smoke_root/dist" -maxdepth 1 -name '*.whl' -print -quit)"
[[ -n "$wheel" ]] || {
    printf 'check-full: built wheel was not found\n' >&2
    exit 1
}

smoke_site="$smoke_root/site"
uv pip install \
    --python .venv/bin/python \
    --no-deps \
    --target "$smoke_site" \
    "$wheel"
H2H_SMOKE_SITE="$smoke_site" \
    H2H_SMOKE_MODULE="hvbattle" \
    .venv/bin/python - <<'PY'
import importlib
import os
import pathlib
import sys

site = pathlib.Path(os.environ["H2H_SMOKE_SITE"]).resolve()
sys.path.insert(0, str(site))
module = importlib.import_module(os.environ["H2H_SMOKE_MODULE"])
module_path = pathlib.Path(module.__file__).resolve()
if not module_path.is_relative_to(site):
    raise SystemExit(f"wheel smoke imported outside target: {module_path}")
PY
