"""Shared classification for failures that safety boundaries must not mask."""

from hvbrowser.runtime import LogPersistenceError


def contains_log_persistence_error(error: BaseException) -> bool:
    """Find a durable-log failure in one bounded exception graph."""

    pending: list[BaseException] = [error]
    visited: set[int] = set()
    while pending and len(visited) < 64:
        candidate = pending.pop()
        identity = id(candidate)
        if identity in visited:
            continue
        visited.add(identity)
        if isinstance(candidate, LogPersistenceError):
            return True
        if isinstance(candidate, BaseExceptionGroup):
            pending.extend(reversed(candidate.exceptions))
        if candidate.__context__ is not None:
            pending.append(candidate.__context__)
        if candidate.__cause__ is not None:
            pending.append(candidate.__cause__)
    return False


__all__ = ["contains_log_persistence_error"]
