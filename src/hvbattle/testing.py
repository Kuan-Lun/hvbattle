"""Explicit in-memory audit adapter for offline tests.

Production composition must use :class:`~hvbattle.DurableAuditEventWriter`.
This module exists so tests that do not own a durable journal never need a
writer-less or silently no-op production bus.
"""

from __future__ import annotations

from typing import Any

from .audit import AuditEvent, AuditEventBus, AuditEventWriter, DurableAuditEventWriter
from .battle_launcher import BattleLauncher
from .hv_battle_action_manager import ElementActionManager
from .hv_battle_ponychart import PonyChart
from .session import BattleSession


class TestingAuditEventBus(AuditEventBus):
    """Synchronous in-memory stand-in with an optional observing callback."""

    __test__ = False

    def __init__(self, writer: AuditEventWriter | None = None) -> None:
        self.events: list[AuditEvent] = []

        def record(event: AuditEvent) -> None:
            self.events.append(event)
            if writer is not None:
                writer(event)

        super().__init__(DurableAuditEventWriter(record))


class TestingBattleSession(BattleSession):
    """Battle session that injects a fresh explicit in-memory audit adapter."""

    __test__ = False

    def __init__(
        self,
        *args: Any,
        audit_event_bus: AuditEventBus | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            *args,
            audit_event_bus=(
                TestingAuditEventBus() if audit_event_bus is None else audit_event_bus
            ),
            **kwargs,
        )


class TestingBattleLauncher(BattleLauncher):
    """Battle launcher with a fresh explicit in-memory audit adapter."""

    __test__ = False

    def __init__(
        self,
        *args: Any,
        audit_event_bus: AuditEventBus | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            *args,
            audit_event_bus=(
                TestingAuditEventBus() if audit_event_bus is None else audit_event_bus
            ),
            **kwargs,
        )


class TestingElementActionManager(ElementActionManager):
    """Action manager with a fresh explicit in-memory audit adapter."""

    __test__ = False

    def __init__(
        self,
        *args: Any,
        audit_event_bus: AuditEventBus | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            *args,
            audit_event_bus=(
                TestingAuditEventBus() if audit_event_bus is None else audit_event_bus
            ),
            **kwargs,
        )


class TestingPonyChart(PonyChart):
    """PonyChart with a fresh explicit in-memory audit adapter."""

    __test__ = False

    def __init__(
        self,
        *args: Any,
        audit_event_bus: AuditEventBus | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            *args,
            audit_event_bus=(
                TestingAuditEventBus() if audit_event_bus is None else audit_event_bus
            ),
            **kwargs,
        )
