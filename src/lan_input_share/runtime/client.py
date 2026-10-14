from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

from lan_input_share.input.events import InputEvent
from lan_input_share.input.key_state import PressedInputState
from lan_input_share.network.session import (
    CloseReason,
    Disconnected,
    MessageReceived,
    SessionEvent,
)
from lan_input_share.protocol.messages import InputMessage, ReleaseInputMessage, WireMessage
from lan_input_share.runtime.controller import decode_input_event


class ClientSession(Protocol):
    def events(self) -> AsyncIterator[SessionEvent]: ...

    async def close(self, reason: CloseReason) -> None: ...


class InputInjector(Protocol):
    def inject(self, event: InputEvent) -> None: ...

    def release_all(self, events: tuple[InputEvent, ...]) -> None: ...


class ClientRuntime:
    def __init__(
        self,
        session: ClientSession,
        injector: InputInjector,
        message_handler: Callable[[WireMessage], Awaitable[None]] | None = None,
    ) -> None:
        self._session = session
        self._injector = injector
        self._pressed = PressedInputState()
        self._task: asyncio.Task[None] | None = None
        self._message_handler = message_handler

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        await self.release_remote_input("shutdown")
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def release_remote_input(self, reason: str) -> None:
        _ = reason
        releases = self._pressed.release_events()
        if releases:
            self._injector.release_all(releases)
        self._pressed.clear()

    async def _run(self) -> None:
        try:
            async for event in self._session.events():
                if isinstance(event, Disconnected):
                    return
                if not isinstance(event, MessageReceived):
                    continue
                message = event.message
                if isinstance(message, InputMessage):
                    input_event = decode_input_event(message.payload)
                    self._injector.inject(input_event)
                    self._pressed.apply(input_event)
                elif isinstance(message, ReleaseInputMessage):
                    await self.release_remote_input(
                        str(message.payload.get("reason", "remote"))
                    )
                elif self._message_handler is not None:
                    await self._message_handler(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._session.close(CloseReason.PROTOCOL_ERROR)
            raise
        finally:
            await self.release_remote_input("consumer_stopped")


__all__ = ["ClientRuntime"]
