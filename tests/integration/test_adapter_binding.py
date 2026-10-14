import socket

import pytest

from lan_input_share.network.adapters import AdapterInfo, bind_socket


@pytest.mark.windows_integration
def test_loopback_binding_uses_selected_source_address():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        adapter = AdapterInfo("loopback", "Loopback", 1, "127.0.0.1", True, 0)
        bind_socket(sock, adapter)
        assert sock.getsockname()[0] == "127.0.0.1"
    finally:
        sock.close()
