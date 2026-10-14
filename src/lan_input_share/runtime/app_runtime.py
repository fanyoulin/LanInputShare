from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from lan_input_share.config.models import AppConfig


class RuntimeComponent(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    kind: str
    detail: str


class AppRuntime:
    def __init__(self) -> None:
        self._config: AppConfig | None = None
        self._component: RuntimeComponent | None = None
        self._statuses: asyncio.Queue[RuntimeStatus] = asyncio.Queue()

    @property
    def config(self) -> AppConfig | None:
        return self._config

    def set_component(self, component: RuntimeComponent) -> None:
        self._component = component

    async def apply_config(self, config: AppConfig) -> None:
        previous = self._component
        if previous is not None:
            await previous.stop()
        self._config = config
        if self._component is not None:
            await self._component.start()
        self.publish_status(RuntimeStatus("config_applied", config.role.value))

    def publish_status(self, status: RuntimeStatus) -> None:
        self._statuses.put_nowait(status)

    async def status_events(self) -> AsyncIterator[RuntimeStatus]:
        while True:
            yield await self._statuses.get()

    async def shutdown(self) -> None:
        if self._component is not None:
            await self._component.stop()
        self._component = None
        self.publish_status(RuntimeStatus("shutdown", "complete"))


__all__ = ["AppRuntime", "RuntimeStatus"]
