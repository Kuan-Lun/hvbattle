"""Private entry point for one owned PonyChart artifact operation."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from ._ponychart_store_process import run_store_child


def _parse_arguments(arguments: Sequence[str] | None) -> tuple[int, str]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("port", type=int)
    parser.add_argument("auth_token")
    namespace = parser.parse_args(arguments)
    return int(namespace.port), str(namespace.auth_token)


def main(arguments: Sequence[str] | None = None) -> int:
    port, auth_token = _parse_arguments(arguments)
    run_store_child(port, auth_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
