from __future__ import annotations

from pdd_rpa.console_auth import ConsoleAuthConfig, ConsoleAuthManager, build_config_document


def _manager(password: str = "Valid-password-123") -> ConsoleAuthManager:
    raw = build_config_document("admin", password, iterations=600_000, session_ttl_seconds=3600)
    return ConsoleAuthManager(
        ConsoleAuthConfig(
            username=raw["username"],
            password_salt=bytes.fromhex(raw["password_salt"]),
            password_hash=bytes.fromhex(raw["password_hash"]),
            password_iterations=raw["password_iterations"],
            session_secret=bytes.fromhex(raw["session_secret"]),
            session_ttl_seconds=raw["session_ttl_seconds"],
        )
    )


def test_console_auth_verifies_username_and_password() -> None:
    manager = _manager()
    assert manager.verify_credentials("admin", "Valid-password-123")
    assert not manager.verify_credentials("other", "Valid-password-123")
    assert not manager.verify_credentials("admin", "wrong")


def test_console_session_is_signed_and_expires() -> None:
    manager = _manager()
    token = manager.issue_session(now=1_000)
    assert manager.validate_session(token, now=1_001)
    assert not manager.validate_session(token + "tampered", now=1_001)
    assert not manager.validate_session(token, now=4_601)
