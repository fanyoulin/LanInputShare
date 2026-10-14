from pathlib import Path

from lan_input_share.windows.firewall import FirewallAdvisor


def test_firewall_command_enables_private_profile_only() -> None:
    command = FirewallAdvisor().private_network_command(
        Path(r"C:\Program Files\LAN Share\lan-input-share.exe"), 24870
    )
    joined = " ".join(command).lower()

    assert command[0].lower().endswith("netsh.exe")
    assert "profile=private" in joined
    assert "profile=public" not in joined
    assert "localport=24870" in joined
    assert "program=c:\\program files\\lan share\\lan-input-share.exe" in joined
