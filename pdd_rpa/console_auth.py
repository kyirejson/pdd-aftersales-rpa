from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_SESSION_TTL_SECONDS = 12 * 60 * 60
MINIMUM_PASSWORD_ITERATIONS = 600_000


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def password_digest(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )


@dataclass(frozen=True)
class ConsoleAuthConfig:
    username: str
    password_salt: bytes
    password_hash: bytes
    password_iterations: int
    session_secret: bytes
    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS

    @classmethod
    def from_file(cls, path: Path) -> "ConsoleAuthConfig":
        try:
            raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            iterations = int(raw["password_iterations"])
            ttl = int(raw.get("session_ttl_seconds", DEFAULT_SESSION_TTL_SECONDS))
            config = cls(
                username=str(raw["username"]).strip(),
                password_salt=bytes.fromhex(str(raw["password_salt"])),
                password_hash=bytes.fromhex(str(raw["password_hash"])),
                password_iterations=iterations,
                session_secret=bytes.fromhex(str(raw["session_secret"])),
                session_ttl_seconds=ttl,
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"控制台访问配置无效：{path}：{exc}") from exc
        if not config.username:
            raise RuntimeError(f"控制台访问配置缺少用户名：{path}")
        if config.password_iterations < MINIMUM_PASSWORD_ITERATIONS:
            raise RuntimeError("控制台密码哈希迭代次数低于安全下限")
        if len(config.password_salt) < 16 or len(config.password_hash) < 32:
            raise RuntimeError("控制台密码哈希配置无效")
        if len(config.session_secret) < 32:
            raise RuntimeError("控制台会话密钥长度不足")
        if not 300 <= config.session_ttl_seconds <= 24 * 60 * 60:
            raise RuntimeError("控制台会话有效期必须在 5 分钟到 24 小时之间")
        return config


class ConsoleAuthManager:
    def __init__(self, config: ConsoleAuthConfig) -> None:
        self.config = config

    def verify_credentials(self, username: str, password: str) -> bool:
        username_ok = hmac.compare_digest(
            username.strip().encode("utf-8"),
            self.config.username.encode("utf-8"),
        )
        candidate = password_digest(
            password,
            self.config.password_salt,
            self.config.password_iterations,
        )
        password_ok = hmac.compare_digest(candidate, self.config.password_hash)
        return username_ok and password_ok

    def issue_session(self, *, now: int | None = None) -> str:
        issued_at = int(time.time() if now is None else now)
        payload = {
            "u": self.config.username,
            "iat": issued_at,
            "exp": issued_at + self.config.session_ttl_seconds,
            "nonce": secrets.token_hex(12),
        }
        encoded = _b64url_encode(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        signature = hmac.new(
            self.config.session_secret,
            encoded.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return f"{encoded}.{_b64url_encode(signature)}"

    def validate_session(self, token: str, *, now: int | None = None) -> bool:
        try:
            encoded, supplied_signature = token.split(".", 1)
            expected_signature = hmac.new(
                self.config.session_secret,
                encoded.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(_b64url_decode(supplied_signature), expected_signature):
                return False
            payload = json.loads(_b64url_decode(encoded).decode("utf-8"))
            current_time = int(time.time() if now is None else now)
            return (
                hmac.compare_digest(str(payload.get("u", "")), self.config.username)
                and int(payload.get("iat", 0)) <= current_time + 60
                and int(payload.get("exp", 0)) > current_time
            )
        except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return False


def build_config_document(
    username: str,
    password: str,
    *,
    iterations: int = MINIMUM_PASSWORD_ITERATIONS,
    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
) -> dict[str, Any]:
    salt = secrets.token_bytes(16)
    return {
        "username": username.strip(),
        "password_salt": salt.hex(),
        "password_hash": password_digest(password, salt, iterations).hex(),
        "password_iterations": iterations,
        "session_secret": secrets.token_bytes(32).hex(),
        "session_ttl_seconds": session_ttl_seconds,
    }
