"""Windows-DPAPI-backed credentials for YTO automatic login.

The encrypted document is deliberately stored outside the project directory so
it is not copied into migration archives.  DPAPI binds it to the current
Windows user; a migrated installation must configure credentials once on the
new computer.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class YtoCredentialError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class YtoCredentials:
    username: str
    password: str


DEFAULT_YTO_CREDENTIAL_FILE = (
    Path.home() / "AppData" / "Local" / "PDD_RPA" / "secrets" / "yto_credentials.dpapi"
)
DEFAULT_YTO_CREDENTIAL_TARGET = "PDD_RPA/YTO_CUSTOMER_SERVICE"
_ENTROPY = b"PDD-RPA:YTO-CREDENTIALS:v1"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _as_blob(value: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(value)
    return (
        _DataBlob(
            len(value),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        ),
        buffer,
    )


def _crypt32() -> tuple[object, object]:
    if os.name != "nt":
        raise YtoCredentialError("圆通凭据加密仅支持 Windows DPAPI")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_wchar_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = ctypes.c_int
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _dpapi_protect(value: bytes) -> bytes:
    crypt32, kernel32 = _crypt32()
    source, source_buffer = _as_blob(value)
    entropy, entropy_buffer = _as_blob(_ENTROPY)
    protected = _DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "PDD RPA YTO credentials",
        ctypes.byref(entropy),
        None,
        None,
        0x1,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(protected),
    ):
        raise YtoCredentialError(f"圆通凭据 DPAPI 加密失败：{ctypes.get_last_error()}")
    del source_buffer, entropy_buffer
    try:
        return ctypes.string_at(protected.pbData, protected.cbData)
    finally:
        kernel32.LocalFree(protected.pbData)


def _dpapi_unprotect(value: bytes) -> bytes:
    crypt32, kernel32 = _crypt32()
    source, source_buffer = _as_blob(value)
    entropy, entropy_buffer = _as_blob(_ENTROPY)
    plain = _DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        ctypes.byref(entropy),
        None,
        None,
        0x1,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(plain),
    ):
        raise YtoCredentialError(
            "圆通凭据无法由当前 Windows 用户解密；请在本机重新配置"
        )
    del source_buffer, entropy_buffer
    try:
        return ctypes.string_at(plain.pbData, plain.cbData)
    finally:
        kernel32.LocalFree(plain.pbData)


def save_yto_credentials(
    path: str | Path,
    username: str,
    password: str,
    *,
    protect: Callable[[bytes], bytes] = _dpapi_protect,
) -> Path:
    account = str(username or "").strip()
    secret = str(password or "")
    if not account or not secret:
        raise YtoCredentialError("圆通账号和密码不能为空")
    destination = Path(path).expanduser()
    document = json.dumps(
        {"username": account, "password": secret},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    encrypted = protect(document)
    payload = json.dumps(
        {
            "version": 1,
            "protection": "windows-dpapi-current-user",
            "payload": base64.b64encode(encrypted).decode("ascii"),
        },
        ensure_ascii=False,
        indent=2,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, destination)
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return destination


def load_yto_credentials(
    path: str | Path,
    *,
    unprotect: Callable[[bytes], bytes] = _dpapi_unprotect,
) -> YtoCredentials:
    source = Path(path).expanduser()
    if not source.is_file():
        raise YtoCredentialError("圆通自动登录凭据尚未在本机配置")
    try:
        outer = json.loads(source.read_text(encoding="utf-8"))
        if int(outer.get("version") or 0) != 1:
            raise ValueError("unsupported version")
        encrypted = base64.b64decode(str(outer["payload"]), validate=True)
        inner = json.loads(unprotect(encrypted).decode("utf-8"))
        credentials = YtoCredentials(
            username=str(inner.get("username") or "").strip(),
            password=str(inner.get("password") or ""),
        )
    except YtoCredentialError:
        raise
    except Exception as exc:
        raise YtoCredentialError("圆通自动登录凭据文件损坏，请在本机重新配置") from exc
    if not credentials.username or not credentials.password:
        raise YtoCredentialError("圆通自动登录凭据不完整，请在本机重新配置")
    return credentials


class _FileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]


class _CredentialAttribute(ctypes.Structure):
    _fields_ = [
        ("Keyword", ctypes.c_wchar_p),
        ("Flags", ctypes.c_ulong),
        ("ValueSize", ctypes.c_ulong),
        ("Value", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _FileTime),
        ("CredentialBlobSize", ctypes.c_ulong),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_ulong),
        ("AttributeCount", ctypes.c_ulong),
        ("Attributes", ctypes.POINTER(_CredentialAttribute)),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


def save_yto_windows_credential(
    username: str,
    password: str,
    *,
    target: str = DEFAULT_YTO_CREDENTIAL_TARGET,
) -> None:
    """Persist YTO login in Windows Credential Manager, without path coupling."""

    account = str(username or "").strip()
    secret = str(password or "")
    if not account or not secret:
        raise YtoCredentialError("圆通账号和密码不能为空")
    if os.name != "nt":
        raise YtoCredentialError("圆通 Windows 凭据存储仅支持 Windows")
    blob = secret.encode("utf-16-le")
    if len(blob) > 5 * 512:
        raise YtoCredentialError("圆通密码长度超过 Windows 凭据限制")
    buffer = ctypes.create_string_buffer(blob)
    credential = _Credential()
    credential.Type = 1  # CRED_TYPE_GENERIC
    credential.TargetName = str(target)
    credential.Comment = "PDD RPA 圆通客户管家自动登录"
    credential.CredentialBlobSize = len(blob)
    credential.CredentialBlob = ctypes.cast(
        buffer, ctypes.POINTER(ctypes.c_ubyte)
    )
    credential.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = account
    advapi32 = ctypes.windll.advapi32
    advapi32.CredWriteW.argtypes = [ctypes.POINTER(_Credential), ctypes.c_ulong]
    advapi32.CredWriteW.restype = ctypes.c_int
    if not advapi32.CredWriteW(ctypes.byref(credential), 0):
        raise YtoCredentialError(
            f"圆通凭据写入 Windows 凭据管理器失败：{ctypes.get_last_error()}"
        )


def load_yto_windows_credential(
    *, target: str = DEFAULT_YTO_CREDENTIAL_TARGET
) -> YtoCredentials:
    if os.name != "nt":
        raise YtoCredentialError("圆通 Windows 凭据存储仅支持 Windows")
    advapi32 = ctypes.windll.advapi32
    pointer = ctypes.POINTER(_Credential)()
    advapi32.CredReadW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.POINTER(_Credential)),
    ]
    advapi32.CredReadW.restype = ctypes.c_int
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    advapi32.CredFree.restype = None
    if not advapi32.CredReadW(str(target), 1, 0, ctypes.byref(pointer)):
        raise YtoCredentialError("Windows 凭据管理器中尚未配置圆通自动登录凭据")
    try:
        item = pointer.contents
        blob = ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize)
        credentials = YtoCredentials(
            username=str(item.UserName or "").strip(),
            password=blob.decode("utf-16-le"),
        )
    except Exception as exc:
        raise YtoCredentialError("Windows 圆通凭据内容损坏，请重新配置") from exc
    finally:
        advapi32.CredFree(pointer)
    if not credentials.username or not credentials.password:
        raise YtoCredentialError("Windows 圆通凭据不完整，请重新配置")
    return credentials


def load_yto_credentials_preferred(
    legacy_path: str | Path = DEFAULT_YTO_CREDENTIAL_FILE,
) -> tuple[YtoCredentials, str]:
    """Load the canonical Windows credential and migrate the old DPAPI file."""

    try:
        return load_yto_windows_credential(), "windows_credential_manager"
    except YtoCredentialError as manager_error:
        try:
            credentials = load_yto_credentials(legacy_path)
        except YtoCredentialError as legacy_error:
            raise YtoCredentialError(
                f"圆通自动登录凭据不可用：{manager_error}；旧凭据：{legacy_error}"
            ) from legacy_error
        try:
            save_yto_windows_credential(credentials.username, credentials.password)
            return credentials, "legacy_dpapi_migrated"
        except YtoCredentialError:
            # The legacy current-user DPAPI value is still safe and usable;
            # report its source so startup diagnostics expose the migration.
            return credentials, "legacy_dpapi_fallback"


__all__ = [
    "DEFAULT_YTO_CREDENTIAL_FILE",
    "DEFAULT_YTO_CREDENTIAL_TARGET",
    "YtoCredentialError",
    "YtoCredentials",
    "load_yto_credentials",
    "load_yto_credentials_preferred",
    "load_yto_windows_credential",
    "save_yto_credentials",
    "save_yto_windows_credential",
]
