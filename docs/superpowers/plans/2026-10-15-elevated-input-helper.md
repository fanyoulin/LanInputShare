# Elevated Input Helper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a narrowly privileged Windows input helper so a normal LanInputShare client can control elevated applications after one explicit setup approval, without elevating the network-facing application.

**Architecture:** The normal client keeps all UI, LAN, clipboard, and file-transfer responsibilities. A separately packaged helper runs at high integrity in the same interactive session, accepts only authenticated fixed-schema input frames over a restricted named pipe, tracks pressed input for safe release, and reports capability state back through the existing control session.

**Tech Stack:** Python 3.13, ctypes/Win32, pywin32 named pipes and Task Scheduler integration, asyncio plus a bounded writer thread, PySide6, PyInstaller, pytest/pytest-asyncio/pytest-qt, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-15-elevated-input-helper-design.md`

## Global Constraints

- Windows 10/11 64-bit only; every computer has one display and the existing LAN topology remains unchanged.
- The main LanInputShare process must remain medium integrity; only `LanInputShareInputHelper.exe` runs high integrity.
- Do not install a kernel driver, Windows Session 0 service, or alter UAC/security-desktop policy.
- UAC secure desktop, Windows logon/lock screen, credentials desktop, Ctrl+Alt+Delete, and protected processes remain unsupported.
- The helper installation target is `%ProgramFiles%\LanInputShare`; the scheduled task must never execute a user-writable desktop/download path.
- Initial enable, repair, upgrade, and removal may request one explicit UAC approval; routine launches must not.
- Helper protocol version is `1`, maximum frame size is `65_536` bytes, heartbeat interval is `1.0` second, and stale-client timeout is `3.0` seconds.
- The client-side helper queue capacity is `1_024`; motion may be coalesced, but key/button transitions and release commands must never be silently dropped.
- Existing TLS pairing, clipboard/file transfer behavior, 4 MiB/s transfer QoS, reconnect behavior, and emergency return hotkey must remain intact.
- Use `apply_patch` for source edits; keep commits scoped and do not add `outputs/` or `work/`.

## Review Focus

- Installed main executable is moved or modified: helper must reject the hash/path mismatch, ordinary input must remain available, and UI must show `需要修复` (Task 1 and Task 4 tests).
- Another Windows user/session connects to the pipe: helper must reject it before accepting input and must not disturb the valid session (Task 2 tests).
- Helper or main client dies while a key/button is held: helper must synthesize releases no later than the 3-second stale timeout (Task 3 tests).
- Mouse flood fills the bridge queue: motion must coalesce while later keyboard/button events preserve ordering and the asyncio runtime must not block (Task 3 tests).
- Main/helper versions differ after an upgrade: no repeated UAC or crash loop; the client falls back and exposes `需要修复` (Task 4 and Task 5 tests).

---

### Task 1: Define the Local Helper Protocol and Authorization Policy

**Files:**
- Create: `src/lan_input_share/elevated/__init__.py`
- Create: `src/lan_input_share/elevated/models.py`
- Create: `src/lan_input_share/elevated/protocol.py`
- Create: `src/lan_input_share/elevated/authorization.py`
- Create: `tests/unit/elevated/test_protocol.py`
- Create: `tests/unit/elevated/test_authorization.py`

**Interfaces:**
- Produces: `HELPER_PROTOCOL_VERSION: int = 1`, `MAX_HELPER_FRAME_BYTES: int = 65_536`.
- Produces: `HelperState` string values `not_installed`, `running`, `repair_required`, `unavailable`, `disabled`, `secure_desktop` and immutable `HelperStatus(state: HelperState, detail: str)`.
- Produces: immutable `ApprovedClient(path: Path, sha256: str, user_sid: str, protocol_version: int)` and `ClientIdentity(pid: int, path: Path, sha256: str, user_sid: str, session_id: int)`.
- Produces: closed `HelperMessage` union for `Hello`, `Input`, `ReleaseAll`, `Ping`, `Status`, and `ShutdownIfIdle`; `encode_helper_message(message: HelperMessage) -> bytes`; and incremental `HelperFrameDecoder.feed(data: bytes) -> tuple[HelperMessage, ...]`.
- Produces: `AuthorizationReason` values `allowed`, `path_mismatch`, `hash_mismatch`, `sid_mismatch`, `session_mismatch`, and `version_mismatch`; immutable `AuthorizationDecision(allowed: bool, reason: AuthorizationReason)`; and `authorize_client(approved: ApprovedClient, actual: ClientIdentity, helper_session_id: int) -> AuthorizationDecision`.

- [ ] **Step 1: Write failing protocol tests**

Cover round trips for `hello`, `input`, `release_all`, `ping`, `status`, and `shutdown_if_idle`; reject unknown kinds, booleans where integers are required, negative sequences, invalid input-event payloads, and frames over exactly `65_536` bytes.

- [ ] **Step 2: Run protocol tests and verify they fail**

Run: `python -m pytest tests/unit/elevated/test_protocol.py -q`

Expected: FAIL because the elevated protocol package does not exist.

- [ ] **Step 3: Implement the typed helper models and bounded length-prefixed codec**

Reuse `encode_input_event()` and `decode_input_event()` for input payloads, but keep helper framing independent from the LAN `WireMessage` session protocol.

- [ ] **Step 4: Run protocol tests and verify they pass**

Run: `python -m pytest tests/unit/elevated/test_protocol.py -q`

Expected: PASS.

- [ ] **Step 5: Write failing authorization tests**

Assert acceptance only when canonical path, uppercase SHA-256, user SID, protocol version, and helper session ID all match. Include modified executable hash, same hash at a moved path, wrong user, wrong session, and version mismatch cases with stable Chinese-facing categories.

- [ ] **Step 6: Implement `authorize_client()` as a pure policy function**

No Win32 calls belong in this module; later tasks supply the observed `ClientIdentity`.

- [ ] **Step 7: Run Task 1 tests and commit**

Run: `python -m pytest tests/unit/elevated/test_protocol.py tests/unit/elevated/test_authorization.py -q`

Expected: PASS.

Commit: `feat: define elevated input helper protocol`

### Task 2: Add Windows Named-Pipe Transport and Process Identity Inspection

**Files:**
- Create: `src/lan_input_share/elevated/pipe_transport.py`
- Create: `src/lan_input_share/windows/process_identity.py`
- Create: `tests/unit/elevated/test_pipe_transport.py`
- Create: `tests/unit/windows/test_process_identity.py`
- Create: `tests/integration/test_elevated_pipe_windows.py`

**Interfaces:**
- Consumes: Task 1 framing and `ClientIdentity`/`ApprovedClient` models.
- Produces: `pipe_name_for(user_sid: str, session_id: int) -> str`.
- Produces: `NamedPipeServer.accept() -> PipeConnection`, `NamedPipeClient.connect(timeout: float) -> PipeConnection`, and `PipeConnection.read/write/close` with bounded frames and cancellation.
- Produces: `inspect_process_identity(pid: int) -> ClientIdentity` and `current_user_sid() -> str`.
- Produces: `default_input_desktop_name() -> str | None`, returning `None` when the input desktop cannot be safely queried.

- [ ] **Step 1: Write failing pipe-name, ACL, cancellation, and process-inspection tests**

Assert that names include normalized SID and session, the security descriptor grants only that SID plus SYSTEM, a blocked accept/read can be closed promptly, and process inspection returns canonical path/hash/SID/session. Add the Review Focus case in which the same SID from another session is rejected before its first input frame.

- [ ] **Step 2: Run unit tests and verify they fail**

Run: `python -m pytest tests/unit/elevated/test_pipe_transport.py tests/unit/windows/test_process_identity.py -q`

Expected: FAIL because transport and inspection modules do not exist.

- [ ] **Step 3: Implement Win32 process inspection and a dependency-injected named-pipe transport**

Use `GetNamedPipeClientProcessId` after connection, `QueryFullProcessImageNameW`, token SID/session inspection, SHA-256 streaming reads, and a pipe DACL for the approved user plus SYSTEM. Keep pywin32 handles behind small adapters so unit tests do not require elevation.

- [ ] **Step 4: Run unit tests, then the Windows pipe integration test**

Run: `python -m pytest tests/unit/elevated/test_pipe_transport.py tests/unit/windows/test_process_identity.py -q`

Run: `python -m pytest tests/integration/test_elevated_pipe_windows.py -q -m windows_integration`

Expected: PASS; the integration test skips with an explicit reason only when Windows pipe APIs are unavailable.

- [ ] **Step 5: Commit**

Commit: `feat: add secure local input pipe transport`

### Task 3: Build the Elevated Helper Runtime and Non-Blocking Client Bridge

**Files:**
- Create: `src/lan_input_share/elevated/server.py`
- Create: `src/lan_input_share/elevated/client.py`
- Create: `src/lan_input_share/elevated/helper_main.py`
- Create: `tests/unit/elevated/test_server.py`
- Create: `tests/unit/elevated/test_client.py`
- Create: `tests/integration/test_elevated_input_helper_windows.py`

**Interfaces:**
- Consumes: Task 1 protocol/authorization and Task 2 pipe/process APIs.
- Produces: `ElevatedInputHelper(approved, injector, pipe_server, *, heartbeat_timeout=3.0)` with `run() -> int` and deterministic `stop()`.
- Produces: `HelperInputBridge(approved, *, capacity=1024, heartbeat_interval=1.0)` implementing the existing synchronous `inject(event)` and `release_all(events)` shape without blocking the asyncio thread.
- Produces: `HelperInputBridge.status -> HelperStatus`, status callbacks, `start()`, and `stop()`.
- Produces: `helper_main(argv: Sequence[str] | None = None) -> int` with no GUI imports.

- [ ] **Step 1: Write failing helper-server tests**

Assert authorization occurs before decoding input, only the fixed message set is handled, injected keys/buttons are tracked, disconnect and 3-second heartbeat expiry release all pressed inputs, `SECURE_DESKTOP` suppresses injection, and malformed frames close only the offending connection.

- [ ] **Step 2: Run helper-server tests and verify they fail**

Run: `python -m pytest tests/unit/elevated/test_server.py -q`

Expected: FAIL because the server does not exist.

- [ ] **Step 3: Implement `ElevatedInputHelper` using `Win32InputInjector` and `PressedInputState`**

Keep a single authorized active client per user/session. Poll input-desktop state on heartbeat/status boundaries rather than on every mouse packet.

- [ ] **Step 4: Write failing bridge tests**

Assert startup handshake, status transitions, exponential reconnect delays from `0.5` to `5.0` seconds, motion coalescing, ordering of key/button transitions, non-blocking `inject()`, no duplicate local injection while connected, and bounded-queue saturation that preserves key/button/release events. Simulate a helper crash while Shift and left mouse are down and assert server-side timeout releases both.

- [ ] **Step 5: Implement `HelperInputBridge` with one writer thread and an urgent release path**

The writer owns the pipe handle. When capacity is reached it may replace a pending mouse-move frame, but it must transition to `UNAVAILABLE` rather than discard a key/button/release transition.

- [ ] **Step 6: Run Task 3 unit and packaged-boundary integration tests**

Run: `python -m pytest tests/unit/elevated/test_server.py tests/unit/elevated/test_client.py -q`

Run: `python -m pytest tests/integration/test_elevated_input_helper_windows.py -q -m windows_integration`

Expected: PASS.

- [ ] **Step 7: Commit**

Commit: `feat: add elevated input helper runtime`

### Task 4: Install, Repair, Start, and Remove the Helper Safely

**Files:**
- Create: `src/lan_input_share/windows/input_helper_install.py`
- Create: `tests/unit/windows/test_input_helper_install.py`
- Modify: `src/lan_input_share/windows/privilege.py`
- Modify: `src/lan_input_share/app.py`
- Modify: `tests/unit/test_app.py`

**Interfaces:**
- Consumes: helper protocol version and `ApprovedClient` manifest fields from Task 1.
- Produces: immutable `HelperInstallStatus(state: HelperState, installed_path: Path | None, detail: str)` and `InputHelperManager.status(main_exe: Path, helper_source: Path) -> HelperInstallStatus`.
- Produces: `InputHelperManager.install(main_exe: Path, helper_source: Path) -> None`, `start() -> None`, and `remove() -> None`.
- Produces: hidden CLI operations `--install-input-helper`, `--remove-input-helper`, and `--input-helper-status`; install/remove operations refuse to run unless elevated.
- Produces: `request_helper_install(...)` and `request_helper_remove(...)` using `ShellExecuteW(..., "runas", ...)` with `subprocess.list2cmdline` argument quoting.

- [ ] **Step 1: Write failing install-manager tests**

Assert `%ProgramFiles%\LanInputShare` destination, atomic helper/manifest replacement, exact approved client path/hash/SID/version, scheduled task name `LanInputShare Elevated Input Helper`, current-user interactive logon, highest run level, explicit argument arrays with `shell=False`, and rollback on partial failure. Reject a source inside Program Files only when its hash is not the approved build artifact.

- [ ] **Step 2: Add failing state tests for moved/modified main EXE and helper version mismatch**

The expected result is `REPAIR_REQUIRED`; status checks must never invoke UAC or automatically rewrite the approved manifest.

- [ ] **Step 3: Run install tests and verify they fail**

Run: `python -m pytest tests/unit/windows/test_input_helper_install.py tests/unit/test_app.py -q`

Expected: FAIL because manager and CLI operations do not exist.

- [ ] **Step 4: Implement the manager, elevated CLI modes, and exact command quoting**

Use a protected manifest beside the helper. Create/start/delete the task without a password and without a shell. Normal `main()` must dispatch hidden maintenance modes before Qt or the single-instance mutex is initialized.

- [ ] **Step 5: Run Task 4 tests and commit**

Run: `python -m pytest tests/unit/windows/test_input_helper_install.py tests/unit/windows/test_privilege.py tests/unit/test_app.py -q`

Expected: PASS.

Commit: `feat: manage elevated input helper installation`

### Task 5: Route Client Input Through the Helper With Safe Fallback

**Files:**
- Create: `src/lan_input_share/input/routed_injector.py`
- Create: `tests/unit/input/test_routed_injector.py`
- Modify: `src/lan_input_share/runtime/client.py`
- Modify: `src/lan_input_share/runtime/service.py`
- Modify: `tests/unit/runtime/test_client_runtime.py`
- Modify: `tests/unit/runtime/test_service.py`

**Interfaces:**
- Consumes: `HelperInputBridge` and `Win32InputInjector`.
- Produces: `RoutedInputInjector(local, helper)` with `inject(event)` and `release_all(events)`.
- Produces: `NetworkRoleService` helper-status callback plumbing without changing controller-role startup.

- [ ] **Step 1: Write failing routed-injector tests**

Assert helper-only injection while `RUNNING`, local fallback for `NOT_INSTALLED`/`UNAVAILABLE`/`REPAIR_REQUIRED`, no local fallback for `SECURE_DESKTOP`, single release delivery, and immediate cleanup when state changes with held inputs.

- [ ] **Step 2: Run routed-injector tests and verify they fail**

Run: `python -m pytest tests/unit/input/test_routed_injector.py -q`

Expected: FAIL because `RoutedInputInjector` does not exist.

- [ ] **Step 3: Implement the routing policy and inject it only for `DeviceRole.CLIENT`**

Controller input capture remains unchanged. Construct/start the bridge before accepting a controller session; stop it after the client runtime has released remote input.

- [ ] **Step 4: Extend client/service lifecycle tests**

Cover normal stop, forced helper disconnect, helper reconnect, main/helper protocol mismatch with continued LAN session, and repeated role/config restarts without duplicate helper threads.

- [ ] **Step 5: Run Task 5 and existing runtime tests**

Run: `python -m pytest tests/unit/input/test_routed_injector.py tests/unit/runtime/test_client_runtime.py tests/unit/runtime/test_service.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

Commit: `feat: route client input through elevated helper`

### Task 6: Surface Capability State Locally and on the Controller

**Files:**
- Modify: `src/lan_input_share/protocol/messages.py`
- Modify: `tests/unit/protocol/test_messages.py`
- Modify: `tests/unit/protocol/test_codec.py`
- Modify: `src/lan_input_share/runtime/service.py`
- Modify: `tests/unit/runtime/test_service.py`
- Modify: `src/lan_input_share/ui/view_model.py`
- Modify: `src/lan_input_share/ui/main_window.py`
- Modify: `tests/unit/ui/test_view_model.py`
- Modify: `tests/unit/ui/test_main_window.py`

**Interfaces:**
- Produces: LAN `InputCapabilityMessage` with payload `{mode, detail}` and increments `PROTOCOL_VERSION` from `2` to `3`.
- Produces: `HelperStatusView(mode: HelperState, text: str, can_enable: bool, can_repair: bool, can_remove: bool)`.
- Produces: runtime operations `enable_input_helper()`, `repair_input_helper()`, and `remove_input_helper()` plus a helper-status callback.

- [ ] **Step 1: Write failing LAN capability-message tests**

Assert version `3`, codec round trip, rejection by an old-version frame, and controller handling for `standard`, `elevated`, `secure_desktop`, `unavailable`, and `repair_required` without disconnecting the input session.

- [ ] **Step 2: Implement the message and service propagation**

Send only on state changes. On the controller, include the reason in the existing connection/slot status so the user can distinguish network loss from Windows input restrictions.

- [ ] **Step 3: Write failing UI tests**

The client-only “增强控制” row must show the five user-visible states, call the correct enable/repair/remove runtime operation, explain that enabling may request UAC once, and state that UAC secure desktop still needs local confirmation. The row must remain hidden for controller role.

- [ ] **Step 4: Implement view-model signals and the client settings controls**

Do not display an extra tray icon or helper window. Never trigger a UAC prompt automatically from a status refresh or reconnect loop.

- [ ] **Step 5: Run Task 6 tests and commit**

Run: `python -m pytest tests/unit/protocol tests/unit/runtime/test_service.py tests/unit/ui/test_view_model.py tests/unit/ui/test_main_window.py -q`

Expected: PASS.

Commit: `feat: show elevated input capability status`

### Task 7: Package, Document, and Validate the Two-Process Release

**Files:**
- Create: `packaging/lan-input-share-input-helper.spec`
- Modify: `packaging/lan-input-share.spec`
- Modify: `scripts/build.ps1`
- Modify: `README.md`
- Modify: `docs/USAGE.zh-CN.md`
- Modify: `docs/TROUBLESHOOTING.zh-CN.md`
- Modify: `docs/acceptance/windows-device-checklist.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: all prior task interfaces.
- Produces: release directory containing `LanInputShare.exe` plus the helper installation artifact, a manifest listing both, and SHA-256 entries for both executables.

- [ ] **Step 1: Add the minimal helper PyInstaller target and build assertions**

The helper target starts at `lan_input_share.elevated.helper_main`, excludes PySide6/clipboard/transfer/network modules, runs a helper smoke test, and is built before the GUI package. The GUI release contains the exact helper artifact that installation approves.

- [ ] **Step 2: Update documentation and acceptance checklist**

Document one-time enable/repair authorization, routine no-prompt startup, state meanings, removal, high-integrity window support, the secure-desktop boundary, protected installation path, and required hash checks.

- [ ] **Step 3: Run the complete source verification suite**

Run: `python -m pytest -q`

Run: `python -m ruff check .`

Run: `python -m mypy src`

Expected: all tests pass with only documented platform skips; Ruff and mypy report no errors.

- [ ] **Step 4: Build and inspect the packaged release**

Run: `powershell -ExecutionPolicy Bypass -File scripts\build.ps1`

Expected: GUI smoke test, helper smoke test, and diagnostics exit `0`; manifest contains no absolute build paths; SHA-256 file contains both executables.

- [ ] **Step 5: Perform local packaged lifecycle tests**

Test install, status, helper start, normal main exit/restart, forced main termination, forced helper termination/recovery, version/hash mismatch, remove/reinstall, and verify only the helper process is high integrity. Record commands and results in the acceptance checklist.

- [ ] **Step 6: Deploy the same complete release to `192.168.31.129` and perform two-machine acceptance**

Verify matching package hashes, ordinary windows, elevated Task Manager/administrator console, local confirmation and recovery around UAC secure desktop, cross-screen return, Shift combinations, clipboard text/image, file transfer QoS, normal/forced exits, and automatic reconnects. Do not claim secure-desktop control.

- [ ] **Step 7: Commit the packaged feature sources and documentation**

Commit: `feat: ship elevated input helper`

- [ ] **Step 8: Run final diff and repository checks**

Run: `git diff --check origin/main...HEAD`

Run: `git status --short`

Expected: no whitespace errors; only intentionally untracked local build/work directories remain; all feature source and docs are committed.
