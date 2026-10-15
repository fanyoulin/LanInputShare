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


def test_helper_install_and_remove_requests_use_fixed_hidden_switches(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    captured: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        privilege,
        "relaunch_elevated",
        lambda argv: captured.append(tuple(argv)),
    )
    executable = r"C:\Program Files\LanInputShare\LanInputShare.exe"

    privilege.request_helper_install(executable)
    privilege.request_helper_remove(executable)

    assert captured == [
        (executable, "--install-input-helper"),
        (executable, "--remove-input-helper"),
    ]
