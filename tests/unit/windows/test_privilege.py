from __future__ import annotations

import lan_input_share.windows.privilege as privilege


def test_elevation_relaunch_preserves_arguments_without_shell_interpolation(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(
        privilege,
        "_shell_execute_elevated",
        lambda executable, parameters: captured.append((executable, parameters)),
    )

    privilege.relaunch_elevated(
        (r"C:\Program Files\LAN Share\lan-input-share.exe", "--name", "A&B desktop")
    )

    assert captured == [
        (
            r"C:\Program Files\LAN Share\lan-input-share.exe",
            ' --name "A&B desktop"'.strip(),
        )
    ]
