from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from lan_input_share.input.events import KeyEvent
from lan_input_share.network.session import CloseReason, Disconnected, MessageReceived, SessionEvent
from lan_input_share.protocol.messages import InputMessage, ReleaseInputMessage
from lan_input_share.runtime.client import ClientRuntime
from lan_input_share.runtime.controller import encode_input_event


class FakeSession:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[SessionEvent] = asyncio.Queue()
        self.closed: list[CloseReason] = []

    async def events(self) -> AsyncIterator[SessionEvent]:
        while True:
            yield await self.queue.get()

    async def close(self, reason: CloseReason) -> None:
        self.closed.append(reason)


class FakeInjector:
    def __init__(self) -> None:
        self.injected: list[object] = []

    def inject(self, event):  # type: ignore[no-untyped-def]
        self.injected.append(event)

    def release_all(self, events):  # type: ignore[no-untyped-def]
        self.injected.extend(events)


@pytest.mark.asyncio
async def test_control_disconnect_releases_client_inputs_before_reconnect() -> None:
    session = FakeSession()
    injector = FakeInjector()
    runtime = ClientRuntime(session, injector)
    await runtime.start()
    down = KeyEvent(30, 65, True)
    await session.queue.put(
        MessageReceived(InputMessage("controller", 1, encode_input_event(down)))
    )
    await asyncio.sleep(0)

    await session.queue.put(Disconnected(CloseReason.HEARTBEAT_TIMEOUT))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert injector.injected == [down, KeyEvent(30, 65, False)]
    assert session.closed == []
    await runtime.stop()


@pytest.mark.asyncio
async def test_explicit_release_message_releases_pressed_inputs() -> None:
    session = FakeSession()
    injector = FakeInjector()
    runtime = ClientRuntime(session, injector)
    await runtime.start()
    down = KeyEvent(30, 65, True)
    await session.queue.put(
        MessageReceived(InputMessage("controller", 1, encode_input_event(down)))
    )
    await session.queue.put(
        MessageReceived(ReleaseInputMessage("controller", 2, {"reason": "switch"}))
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert injector.injected[-1] == KeyEvent(30, 65, False)
    await runtime.stop()


@pytest.mark.asyncio
async def test_message_processing_failure_still_releases_pressed_inputs() -> None:
    session = FakeSession()
    injector = FakeInjector()
    runtime = ClientRuntime(session, injector)
    await runtime.start()
    down = KeyEvent(30, 65, True)
    await session.queue.put(
        MessageReceived(InputMessage("controller", 1, encode_input_event(down)))
    )
    await session.queue.put(
        MessageReceived(InputMessage("controller", 2, {"kind": "invalid"}))
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert injector.injected == [down, KeyEvent(30, 65, False)]
    assert session.closed == [CloseReason.PROTOCOL_ERROR]
    await runtime.stop()
