from __future__ import annotations

import pytest

from lan_input_share.app import ApplicationCoordinator


class Resource:
    def __init__(self, name: str, events: list[str], fail: bool = False) -> None:
        self.name = name
        self.events = events
        self.fail = fail

    async def start(self) -> None:
        self.events.append(f"start:{self.name}")
        if self.fail:
            raise RuntimeError("fatal runtime error")

    async def stop(self) -> None:
        self.events.append(f"stop:{self.name}")


class Safety:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def restore_local_input(self) -> None:
        self.events.append("restore")


class Guard:
    def acquire(self) -> bool:
        return True

    def focus_existing(self) -> None:
        pass

    def release(self) -> None:
        pass


@pytest.mark.asyncio
async def test_unhandled_runtime_error_restores_local_input_before_exit() -> None:
    events: list[str] = []
    app = ApplicationCoordinator(
        [Resource("network", events), Resource("runtime", events, fail=True)],
        Safety(events),
        Guard(),
    )

    with pytest.raises(RuntimeError, match="fatal"):
        await app.start()

    assert events == ["start:network", "start:runtime", "restore", "stop:network"]
