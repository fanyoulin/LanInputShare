import logging

from lan_input_share.logging_setup import (
    configure_logging,
    redact_log_value,
    sanitize_transfer_error,
)


def test_log_redaction_removes_clipboard_text_private_key_and_auth_token():
    assert redact_log_value("clipboard_text=secret words") == "clipboard_text=[REDACTED]"
    assert redact_log_value("auth_token=abc123") == "auth_token=[REDACTED]"
    assert redact_log_value(
        "private_key=-----BEGIN PRIVATE KEY-----abc-----END PRIVATE KEY-----"
    ) == "private_key=[REDACTED]"


def test_transfer_error_sanitizer_never_returns_path_or_conflict_name() -> None:
    error = r"源文件在传输中发生变化：C:\Users\secret\TOP-SECRET-NAME.bin"

    result = sanitize_transfer_error(error)

    assert result == "源文件已变化"
    assert "secret" not in result
    assert "TOP-SECRET" not in result


def test_redact_log_value_hides_windows_absolute_paths() -> None:
    result = redact_log_value(
        r"transfer failed at C:\Users\secret\TOP-SECRET-NAME.bin"
    )

    assert "C:\\Users" not in result
    assert "TOP-SECRET" not in result


def test_configure_logging_writes_utf8_without_duplicate_handlers(tmp_path):
    logger = configure_logging(tmp_path)
    same_logger = configure_logging(tmp_path)

    logger.info("连接成功：主控机")
    for handler in logger.handlers:
        handler.flush()

    assert same_logger is logger
    assert len(logger.handlers) == 1
    assert (tmp_path / "lan-input-share.log").read_text(encoding="utf-8").endswith(
        "连接成功：主控机\n"
    )
    assert logger.propagate is False
    assert logger.level == logging.INFO
