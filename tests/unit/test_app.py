from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import lan_input_share.app as app
from lan_input_share.elevated.models import HelperState


class FakeManager:
    installs: list[tuple[Path, Path]] = []
    starts = 0
    removes = 0

    def install(self, main_exe: Path, helper_source: Path) -> None:
        self.installs.append((main_exe, helper_source))

    def start(self) -> None:
        type(self).starts += 1

    def remove(self) -> None:
        type(self).removes += 1

    def status(self, main_exe: Path, helper_source: Path) -> object:
        return SimpleNamespace(
            state=HelperState.NOT_INSTALLED,
            installed_path=None,
            detail=f"{main_exe.name}:{helper_source.name}",
        )


def test_install_helper_refuses_to_run_without_elevation(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    FakeManager.installs.clear()
    FakeManager.starts = 0
    monkeypatch.setattr(app, "is_elevated", lambda: False)
    monkeypatch.setattr(app, "InputHelperManager", FakeManager)

    assert app.main(["--install-input-helper"]) == 2
    assert FakeManager.installs == []
    assert FakeManager.starts == 0


def test_install_helper_dispatches_before_gui(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    executable = tmp_path / "LanInputShare.exe"
    FakeManager.installs.clear()
    FakeManager.starts = 0
    monkeypatch.setattr(app, "is_elevated", lambda: True)
    monkeypatch.setattr(app, "InputHelperManager", FakeManager)
    monkeypatch.setattr(app, "_current_executable", lambda: executable)
    monkeypatch.setattr(
        app,
        "_run_gui",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("GUI started")),
    )

    assert app.main(["--install-input-helper"]) == 0
    assert FakeManager.installs == [
        (executable, executable.with_name("LanInputShareInputHelper.exe"))
    ]
    assert FakeManager.starts == 1


def test_remove_helper_requires_elevation_and_dispatches(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    FakeManager.removes = 0
    monkeypatch.setattr(app, "InputHelperManager", FakeManager)
    monkeypatch.setattr(app, "is_elevated", lambda: True)

    assert app.main(["--remove-input-helper"]) == 0
    assert FakeManager.removes == 1


def test_helper_status_is_read_only_and_does_not_require_elevation(
    monkeypatch,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(app, "InputHelperManager", FakeManager)
    monkeypatch.setattr(
        app,
        "is_elevated",
        lambda: (_ for _ in ()).throw(AssertionError("elevation checked")),
    )

    assert app.main(["--input-helper-status"]) == 0
    assert '"state": "not_installed"' in capsys.readouterr().out
