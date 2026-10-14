from __future__ import annotations

import asyncio

import pytest

from lan_input_share.config.paths import AppPaths
from lan_input_share.network.connection import StreamTransport
from lan_input_share.security.identity import DeviceIdentity
from lan_input_share.security.tls import create_client_ssl_context, create_server_ssl_context


@pytest.mark.asyncio
async def test_tls_channel_binding_matches_on_both_ends(tmp_path) -> None:  # type: ignore[no-untyped-def]
    paths = AppPaths(
        tmp_path,
        tmp_path / "config.json",
        tmp_path / "logs",
        tmp_path / "identity",
        tmp_path / "staging",
    )
    identity = DeviceIdentity.load_or_create(paths, "controller")
    server_binding: asyncio.Future[bytes | None] = asyncio.Future()

    async def accepted(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        transport = StreamTransport(reader, writer)
        server_binding.set_result(transport.channel_binding())
        await transport.close()

    server = await asyncio.start_server(
        accepted,
        "127.0.0.1",
        0,
        ssl=create_server_ssl_context(identity),
    )
    try:
        port = int(server.sockets[0].getsockname()[1])
        reader, writer = await asyncio.open_connection(
            "127.0.0.1",
            port,
            ssl=create_client_ssl_context(),
        )
        client = StreamTransport(reader, writer)
        client_binding = client.channel_binding()
        peer_binding = await asyncio.wait_for(server_binding, timeout=2)

        assert client_binding
        assert peer_binding == client_binding
        await client.close()
    finally:
        server.close()
        await server.wait_closed()
