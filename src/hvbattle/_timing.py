"""Internal timing primitives that keep semantic waits out of CDP calls."""

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass

PROTOCOL_TIMEOUT_SECONDS = 5.0


def protocol_timeout(available: float = PROTOCOL_TIMEOUT_SECONDS) -> float:
    """Return a valid per-transaction watchdog bounded by the protocol cap."""
    if not math.isfinite(available) or available <= 0:
        raise TimeoutError("No protocol transaction time remains")
    return min(PROTOCOL_TIMEOUT_SECONDS, available)


@dataclass(frozen=True, slots=True)
class SemanticDeadline:
    """A local end-to-end deadline whose budget is never a CDP timeout."""

    expires_at: float
    _clock: Callable[[], float]

    @classmethod
    def after(
        cls,
        seconds: float,
        *,
        clock: Callable[[], float] | None = None,
    ) -> SemanticDeadline:
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("semantic deadline must be a finite positive number")
        active_clock = clock or asyncio.get_running_loop().time
        return cls(active_clock() + seconds, active_clock)

    def remaining(self) -> float:
        return self.expires_at - self._clock()

    def capped(self, seconds: float) -> SemanticDeadline:
        """Return a child deadline that cannot outlive this deadline."""
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("deadline cap must be a finite positive number")
        return SemanticDeadline(
            expires_at=min(self.expires_at, self._clock() + seconds),
            _clock=self._clock,
        )

    def protocol_timeout(self) -> float:
        return protocol_timeout(self.remaining())

    def require_remaining(self, message: str) -> float:
        remaining = self.remaining()
        if remaining <= 0:
            raise TimeoutError(message)
        return remaining


__all__ = ["PROTOCOL_TIMEOUT_SECONDS", "SemanticDeadline", "protocol_timeout"]
