"""Private subprocess entry points for supervised PonyChart workers."""

from __future__ import annotations

import socket
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from ._ponychart_workers import (
    PonyChartGenerationDescriptor,
    _inference_worker_main,
    _InferenceChannel,
    _retention_supervised_worker_main,
    _WorkerHello,
)


def _connect(host: str, raw_port: str, token: str) -> _InferenceChannel:
    port = int(raw_port)
    if host != "127.0.0.1" or port <= 0 or port > 65_535 or not token:
        raise ValueError("invalid PonyChart worker IPC arguments")
    expires_at = time.monotonic() + 5.0
    transport = socket.create_connection((host, port), timeout=5.0)
    connection = _InferenceChannel(transport, token)
    connection.send(_WorkerHello(token), expires_at=expires_at)
    return connection


def main(arguments: Sequence[str] | None = None) -> int:
    values = tuple(sys.argv[1:] if arguments is None else arguments)
    try:
        if len(values) == 7 and values[0] == "inference":
            _, host, raw_port, token, generation, model_path, thresholds_path = values
            connection = _connect(host, raw_port, token)
            _inference_worker_main(
                connection,
                PonyChartGenerationDescriptor(
                    generation=generation,
                    model_path=Path(model_path),
                    thresholds_path=Path(thresholds_path),
                ),
            )
        elif len(values) == 4 and values[0] == "retention":
            _, host, raw_port, token = values
            port = int(raw_port)
            if host != "127.0.0.1" or port <= 0 or port > 65_535 or not token:
                raise ValueError("invalid PonyChart retention IPC arguments")
            transport = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                transport.connect((host, port))
                _retention_supervised_worker_main(transport, token=token)
            finally:
                transport.close()
        else:
            return 2
    except OSError, RuntimeError, ValueError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
