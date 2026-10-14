from __future__ import annotations

from pathlib import Path

import pytest

from lan_input_share.clipboard.remote_files import RemoteFileClipboard
from lan_input_share.transfer.manifest import build_manifest
from lan_input_share.transfer.protocol import TransferResult


@pytest.mark.asyncio
async def test_ctrl_x_is_advertised_and_executed_as_copy_only(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("keep me", encoding="utf-8")
    manifest = build_manifest([source])
    staged = tmp_path / "staged.txt"

    async def transfer(_manifest, _source, _target):  # type: ignore[no-untyped-def]
        staged.write_text("keep me", encoding="utf-8")
        return TransferResult(manifest.transfer_id, True, 7, 1, (staged,))

    clipboard_paths: list[tuple[Path, ...]] = []
    replayed: list[bool] = []
    remote = RemoteFileClipboard(transfer, clipboard_paths.append, lambda: replayed.append(True))
    remote.copied(manifest, "source", cut=True)

    outcome = await remote.handle_paste("target")

    assert outcome.success and outcome.copy_only
    assert source.exists()
    assert clipboard_paths == [(staged,)]
    assert replayed == [True]


@pytest.mark.asyncio
async def test_success_sets_local_cf_hdrop_then_replays_one_paste(tmp_path: Path) -> None:
    source = tmp_path / "a.txt"
    source.write_text("a", encoding="utf-8")
    manifest = build_manifest([source])
    order: list[str] = []

    async def transfer(_manifest, _source, _target):  # type: ignore[no-untyped-def]
        return TransferResult(manifest.transfer_id, True, 1, 1, (tmp_path / "done" / "a.txt",))

    remote = RemoteFileClipboard(
        transfer,
        lambda _paths: order.append("clipboard"),
        lambda: order.append("paste"),
    )
    remote.copied(manifest, "source")

    first = await remote.handle_paste("target")
    second = await remote.handle_paste("target")

    assert first.replayed is True
    assert second.replayed is False
    assert order == ["clipboard", "paste"]


@pytest.mark.asyncio
async def test_same_device_paste_is_never_intercepted_or_transferred(tmp_path: Path) -> None:
    source = tmp_path / "local.zip"
    source.write_bytes(b"zip")
    manifest = build_manifest([source])
    transfers: list[bool] = []

    async def transfer(_manifest, _source, _target):  # type: ignore[no-untyped-def]
        transfers.append(True)
        return TransferResult(manifest.transfer_id, True, 3, 1, (source,))

    remote = RemoteFileClipboard(transfer, lambda _paths: None, lambda: None)
    remote.copied(manifest, "client-a")

    outcome = await remote.handle_paste("client-a")

    assert outcome.suppressed is False
    assert outcome.replayed is False
    assert transfers == []
