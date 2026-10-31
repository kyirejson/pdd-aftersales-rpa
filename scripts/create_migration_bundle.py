"""Create a live-safe, self-contained migration bundle on another drive.

The source project is read-only.  SQLite is copied through the online backup
API, so the existing console and RPA processes do not need to be stopped.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_FORMAT_VERSION = 2
PYTHON_VERSION = "3.13.14"
PYTHON_INSTALLER_NAME = f"python-{PYTHON_VERSION}-amd64.exe"
PYTHON_INSTALLER_URL = (
    f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{PYTHON_INSTALLER_NAME}"
)

SOURCE_EXCLUDED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "runtime",
}
SOURCE_EXCLUDED_SUFFIXES = {".db", ".pyc", ".pyo"}
PROFILE_CACHE_DIRECTORIES = {
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnCache",
    "GrShaderCache",
    "ShaderCache",
    "Crashpad",
    "BrowserMetrics",
    "optimization_guide_model_store",
    "Safe Browsing",
}
PROFILE_TRANSIENT_FILES = {
    "LOCK",
    "SingletonCookie",
    "SingletonLock",
    "SingletonSocket",
    "DevToolsActivePort",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_copy_file(source: Path, target: Path, skipped: list[dict[str, str]]) -> bool:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return True
    except OSError as exc:
        skipped.append({"path": str(source), "error": str(exc)})
        return False


def _copy_tree(
    source: Path,
    target: Path,
    *,
    skip_directory: Callable[[Path], bool] | None = None,
    skip_file: Callable[[Path], bool] | None = None,
    skipped: list[dict[str, str]] | None = None,
) -> dict[str, int]:
    skipped = skipped if skipped is not None else []
    counts = {"files": 0, "bytes": 0}
    if not source.is_dir():
        return counts
    for current, directories, files in os.walk(source):
        current_path = Path(current)
        directories[:] = [
            name
            for name in directories
            if not skip_directory or not skip_directory(current_path / name)
        ]
        relative = current_path.relative_to(source)
        (target / relative).mkdir(parents=True, exist_ok=True)
        for name in files:
            item = current_path / name
            if skip_file and skip_file(item):
                continue
            destination = target / relative / name
            if _safe_copy_file(item, destination, skipped):
                counts["files"] += 1
                counts["bytes"] += item.stat().st_size
    return counts


def _copy_source(project_target: Path, skipped: list[dict[str, str]]) -> dict[str, int]:
    def skip_directory(path: Path) -> bool:
        return path.name in SOURCE_EXCLUDED_DIRECTORIES

    def skip_file(path: Path) -> bool:
        return path.suffix.lower() in SOURCE_EXCLUDED_SUFFIXES

    return _copy_tree(
        PROJECT_ROOT,
        project_target,
        skip_directory=skip_directory,
        skip_file=skip_file,
        skipped=skipped,
    )


def _sqlite_online_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.backup(target_connection, pages=512, sleep=0.01)
        integrity = target_connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"数据库快照完整性检查失败：{integrity}")
    finally:
        target_connection.close()
        source_connection.close()


def _force_safe_snapshot(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        now = datetime.now().isoformat(timespec="seconds")
        connection.execute(
            "INSERT INTO system_settings(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            ("global_paused", "true", now),
        )
        row = connection.execute(
            "SELECT value FROM system_settings WHERE key='runtime_controller_status'"
        ).fetchone()
        try:
            status = dict(json.loads(row[0]) or {}) if row else {}
        except (TypeError, ValueError):
            status = {}
        status.update(
            {
                "state": "STOPPED",
                "phase": "migration_bundle_ready",
                "current_task": None,
                "next_sync_at": "",
                "recovery_pending": False,
                "recovery_active": False,
                "last_error": "",
                "stop_requested_at": now,
            }
        )
        connection.execute(
            "INSERT INTO system_settings(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (
                "runtime_controller_status",
                json.dumps(status, ensure_ascii=False),
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _query_distribution(
    connection: sqlite3.Connection, query: str
) -> list[dict[str, Any]]:
    try:
        return [
            {"value": str(row[0] or ""), "count": int(row[1] or 0)}
            for row in connection.execute(query)
        ]
    except sqlite3.DatabaseError:
        return []


def _database_inventory(database: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        table_names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        tables = []
        for table in table_names:
            quoted = '"' + table.replace('"', '""') + '"'
            count = int(connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
            tables.append({"name": table, "rows": count})

        settings: dict[str, str] = {}
        if "system_settings" in table_names:
            settings = {
                str(key): str(value or "")
                for key, value in connection.execute(
                    "SELECT key, value FROM system_settings "
                    "WHERE key IN ('global_paused', 'runtime_controller_status')"
                )
            }
        runtime_status: dict[str, Any] = {}
        try:
            runtime_status = dict(
                json.loads(settings.get("runtime_controller_status") or "{}") or {}
            )
        except (TypeError, ValueError):
            pass

        return {
            "integrity": integrity,
            "bytes": database.stat().st_size,
            "page_count": page_count,
            "page_size": page_size,
            "table_count": len(tables),
            "tables": tables,
            "global_paused": settings.get("global_paused", "").lower() == "true",
            "runtime_state": str(runtime_status.get("state") or ""),
            "runtime_phase": str(runtime_status.get("phase") or ""),
            "task_states": _query_distribution(
                connection,
                "SELECT state, COUNT(*) FROM tasks GROUP BY state ORDER BY COUNT(*) DESC",
            ),
            "task_shops": _query_distribution(
                connection,
                "SELECT shop_id, COUNT(*) FROM tasks GROUP BY shop_id ORDER BY COUNT(*) DESC",
            ),
            "action_statuses": _query_distribution(
                connection,
                "SELECT status, COUNT(*) FROM action_logs "
                "GROUP BY status ORDER BY COUNT(*) DESC",
            ),
            "evidence_types": _query_distribution(
                connection,
                "SELECT evidence_type, COUNT(*) FROM task_evidence "
                "GROUP BY evidence_type ORDER BY COUNT(*) DESC",
            ),
            "dingtalk_targets": _query_distribution(
                connection,
                "SELECT dingtalk_target_key, COUNT(*) FROM interception_registers "
                "GROUP BY dingtalk_target_key ORDER BY COUNT(*) DESC",
            ),
        }
    finally:
        connection.close()


def _directory_inventory(path: Path) -> dict[str, Any]:
    files = [item for item in path.rglob("*") if item.is_file()] if path.is_dir() else []
    return {
        "path": str(path),
        "files": len(files),
        "bytes": sum(item.stat().st_size for item in files),
    }


def _copy_runtime(
    bundle: Path,
    project_target: Path,
) -> dict[str, Any]:
    source_runtime = PROJECT_ROOT / "runtime"
    target_runtime = project_target / "runtime"
    target_runtime.mkdir(parents=True, exist_ok=True)
    raw_database = bundle / "machine_bound_backup" / "database" / "rpa.source_snapshot.db"
    deployed_database = target_runtime / "rpa.db"
    _sqlite_online_backup(source_runtime / "rpa.db", raw_database)
    shutil.copy2(raw_database, deployed_database)
    _force_safe_snapshot(deployed_database)

    evidence_skipped: list[dict[str, str]] = []
    evidence = _copy_tree(
        source_runtime / "evidence",
        target_runtime / "evidence",
        skipped=evidence_skipped,
    )
    if evidence_skipped:
        raise RuntimeError(
            f"业务证据复制不完整，拒绝生成迁移包：{len(evidence_skipped)} 个文件"
        )

    root_data_files: list[dict[str, Any]] = []
    root_data_skipped: list[dict[str, str]] = []
    for item in source_runtime.iterdir():
        if not item.is_file():
            continue
        if item.suffix.lower() not in {".xlsx", ".ndjson", ".png"}:
            continue
        if _safe_copy_file(item, target_runtime / item.name, root_data_skipped):
            root_data_files.append(
                {"name": item.name, "bytes": item.stat().st_size, "sha256": _sha256(item)}
            )
    if root_data_skipped:
        raise RuntimeError(
            f"根目录业务数据复制不完整，拒绝生成迁移包：{len(root_data_skipped)} 个文件"
        )

    diagnostics = bundle / "diagnostics" / "runtime_logs"
    diagnostics.mkdir(parents=True, exist_ok=True)
    diagnostic_skipped: list[dict[str, str]] = []
    log_files: list[dict[str, Any]] = []
    for item in source_runtime.iterdir():
        if not item.is_file():
            continue
        if item.suffix.lower() not in {".log", ".txt"}:
            continue
        if _safe_copy_file(item, diagnostics / item.name, diagnostic_skipped):
            log_files.append({"name": item.name, "bytes": item.stat().st_size})

    edge_profiles_target = target_runtime / "edge_profiles"
    (edge_profiles_target / "dingtalk_playwright").mkdir(parents=True, exist_ok=True)
    (edge_profiles_target / "yto_playwright").mkdir(parents=True, exist_ok=True)
    (edge_profiles_target / "qianniu_edge").mkdir(parents=True, exist_ok=True)
    (edge_profiles_target / "README.txt").write_text(
        "此目录故意不部署源电脑登录态。请在新电脑分别打开钉钉、圆通和千牛独立窗口后重新登录。\n"
        "machine_bound_backup 中的浏览器目录只用于留档，禁止覆盖到这里。\n",
        encoding="utf-8",
    )

    profile_skipped: list[dict[str, str]] = []

    def skip_profile_directory(path: Path) -> bool:
        return path.name in PROFILE_CACHE_DIRECTORIES

    def skip_profile_file(path: Path) -> bool:
        return path.name in PROFILE_TRANSIENT_FILES or path.suffix.lower() == ".tmp"

    profile_copy = _copy_tree(
        source_runtime / "edge_profiles",
        bundle / "machine_bound_backup" / "edge_profiles",
        skip_directory=skip_profile_directory,
        skip_file=skip_profile_file,
        skipped=profile_skipped,
    )

    raw_inventory = _database_inventory(raw_database)
    deployed_inventory = _database_inventory(deployed_database)
    if raw_inventory["integrity"] != "ok" or deployed_inventory["integrity"] != "ok":
        raise RuntimeError("数据库快照完整性检查失败")
    if not deployed_inventory["global_paused"] or deployed_inventory["runtime_state"] != "STOPPED":
        raise RuntimeError("部署数据库未处于全局暂停 + STOPPED 安全状态")

    return {
        "source_database": str(source_runtime / "rpa.db"),
        "snapshot_created_at": datetime.now().isoformat(timespec="seconds"),
        "raw_database_sha256": _sha256(raw_database),
        "deployed_database_sha256": _sha256(deployed_database),
        "database_safety": {"global_paused": True, "runtime_state": "STOPPED"},
        "raw_database_inventory": raw_inventory,
        "deployed_database_inventory": deployed_inventory,
        "evidence": evidence,
        "root_data_files": root_data_files,
        "diagnostic_logs": log_files,
        "diagnostic_copy_warnings": diagnostic_skipped,
        "machine_bound_edge_profiles": profile_copy,
        "machine_bound_profile_sections": {
            item.name: _directory_inventory(item)
            for item in sorted((bundle / "machine_bound_backup" / "edge_profiles").iterdir())
            if item.is_dir()
        },
        "machine_bound_profile_skipped": profile_skipped,
    }


def _validate_offline_assets(offline: Path) -> dict[str, Any]:
    installer = offline / PYTHON_INSTALLER_NAME
    wheelhouse = offline / "wheelhouse"
    if not installer.is_file() or installer.stat().st_size < 10 * 1024 * 1024:
        raise RuntimeError(f"Python 离线安装包缺失或大小异常：{installer}")
    wheels = sorted(wheelhouse.glob("*.whl"))
    if not wheels:
        raise RuntimeError(f"离线 wheelhouse 为空：{wheelhouse}")

    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--dry-run",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        str(wheelhouse),
        "-r",
        str(PROJECT_ROOT / "requirements.lock.txt"),
    ]
    check = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if check.returncode != 0:
        detail = (check.stderr or check.stdout or "").strip()[-4000:]
        raise RuntimeError(f"离线依赖闭包验证失败：\n{detail}")

    return {
        "python_installer": {
            "name": installer.name,
            "size": installer.stat().st_size,
            "sha256": _sha256(installer),
        },
        "wheel_count": len(wheels),
        "wheel_bytes": sum(item.stat().st_size for item in wheels),
        "wheelhouse_validation": {
            "ok": True,
            "command": "pip install --dry-run --ignore-installed --no-index",
        },
    }


def _prepare_offline_assets(
    bundle: Path, offline_source: Path | None = None
) -> dict[str, Any]:
    offline = bundle / "offline"
    wheelhouse = offline / "wheelhouse"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    if offline_source and offline_source.is_dir():
        skipped: list[dict[str, str]] = []
        copied = _copy_tree(offline_source, offline, skipped=skipped)
        if skipped:
            raise RuntimeError(
                f"复用离线资源时有 {len(skipped)} 个文件复制失败，拒绝继续"
            )
        result = _validate_offline_assets(offline)
        result.update(
            {
                "mode": "reused_and_validated",
                "source": str(offline_source.resolve()),
                "copy": copied,
            }
        )
        return result

    lock_file = PROJECT_ROOT / "requirements.lock.txt"
    command = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--only-binary=:all:",
        "--dest",
        str(wheelhouse),
        "-r",
        str(lock_file),
    ]
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)

    installer = offline / PYTHON_INSTALLER_NAME
    request = urllib.request.Request(
        PYTHON_INSTALLER_URL,
        headers={"User-Agent": "PDD-RPA-Migration-Builder/1.0"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        with installer.open("wb") as target:
            shutil.copyfileobj(response, target)
    result = _validate_offline_assets(offline)
    result.update(
        {
            "mode": "downloaded_and_validated",
            "source": PYTHON_INSTALLER_URL,
        }
    )
    return result


def _display_metrics() -> dict[str, int]:
    if os.name != "nt":
        return {}
    user32 = ctypes.windll.user32
    dpi = 96
    try:
        dpi = int(user32.GetDpiForSystem())
    except Exception:
        pass
    return {
        "width": int(user32.GetSystemMetrics(0)),
        "height": int(user32.GetSystemMetrics(1)),
        "dpi": dpi,
        "scale_percent": round(dpi / 96 * 100),
    }


def _edge_info() -> dict[str, Any]:
    candidates = (
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    )
    edge = next((item for item in candidates if item.is_file()), None)
    version = ""
    if edge:
        try:
            import win32api

            info = win32api.GetFileVersionInfo(str(edge), "\\")
            ms = info["FileVersionMS"]
            ls = info["FileVersionLS"]
            version = ".".join(
                map(str, (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF))
            )
        except Exception:
            pass
    return {"path": str(edge or ""), "version": version}


def _source_application_paths() -> dict[str, str]:
    program_files = Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
    program_files_x86 = Path(
        os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")
    )
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = {
        "wdt": (
            Path("D:/旺店通/旺店通ERP旗舰版/旺店通ERP旗舰版.exe"),
            Path("D:/旺店通ERP旗舰版/旺店通ERP旗舰版.exe"),
            program_files / "旺店通/旺店通ERP旗舰版.exe",
            program_files_x86 / "旺店通/旺店通ERP旗舰版.exe",
        ),
        "wecom": (
            Path("D:/WXWork/WXWork.exe"),
            Path("D:/Program Files/WXWork/WXWork.exe"),
            program_files / "WXWork/WXWork.exe",
            program_files_x86 / "WXWork/WXWork.exe",
        ),
        "dingtalk": (
            Path("D:/DINGDING/DingDing/main/current/DingTalk.exe"),
            program_files / "DingDing/main/current/DingTalk.exe",
            program_files_x86 / "DingDing/main/current/DingTalk.exe",
        ),
        "wechat": (
            Path("D:/WX/Weixin/Weixin.exe"),
            program_files / "Tencent/WeChat/WeChat.exe",
            program_files_x86 / "Tencent/WeChat/WeChat.exe",
            local_app_data / "Tencent/WeChat/WeChat.exe",
            local_app_data / "Tencent/xwechat/xwechat.exe",
        ),
    }
    return {
        key: str(next((item for item in values if item.is_file()), ""))
        for key, values in candidates.items()
    }


def _configuration_inventory() -> dict[str, Any]:
    settings_path = PROJECT_ROOT / "config" / "settings.json"
    lock_path = PROJECT_ROOT / "requirements.lock.txt"
    settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    qianniu = dict(settings.get("qianniu_shop_browser") or {})
    dingtalk = dict(settings.get("dingtalk_interception_register") or {})
    return {
        "settings_sha256": _sha256(settings_path),
        "requirements_lock_sha256": _sha256(lock_path),
        "execution_mode": str(settings.get("execution_mode") or ""),
        "live_actions_enabled": bool(settings.get("live_actions_enabled")),
        "continuous": bool(dict(settings.get("runtime") or {}).get("continuous")),
        "pdd_shop_count": len(settings.get("shops") or []),
        "enabled_pdd_shop_count": sum(
            1 for shop in settings.get("shops") or [] if shop.get("enabled", True)
        ),
        "qianniu": {
            "enabled": bool(qianniu.get("enabled")),
            "workflow_enabled": bool(qianniu.get("workflow_enabled")),
            "production_enabled": bool(qianniu.get("production_enabled")),
            "id": str(qianniu.get("id") or ""),
            "shop_name": str(qianniu.get("shop_name") or ""),
            "user_data_dir": str(qianniu.get("user_data_dir") or ""),
        },
        "dingtalk_targets": [
            {
                "key": key,
                "warehouse_aliases": list(dict(value or {}).get("warehouse_aliases") or []),
                "sheet_name": str(dict(value or {}).get("sheet_name") or ""),
                "layout": str(dict(value or {}).get("layout") or ""),
            }
            for key, value in dict(dingtalk.get("targets") or {}).items()
        ],
        "safety": dict(settings.get("safety") or {}),
    }


def _write_checksums(bundle: Path) -> dict[str, Any]:
    checksum_path = bundle / "SHA256SUMS.txt"
    detached_path = bundle / "SHA256SUMS.sha256"
    files = sorted(
        item
        for item in bundle.rglob("*")
        if item.is_file() and item not in {checksum_path, detached_path}
    )
    with checksum_path.open("w", encoding="utf-8", newline="\n") as handle:
        for index, item in enumerate(files, start=1):
            relative = item.relative_to(bundle).as_posix()
            handle.write(f"{_sha256(item)}  {relative}\n")
            if index % 500 == 0:
                handle.flush()
    checksum_sha256 = _sha256(checksum_path)
    detached_path.write_text(
        f"{checksum_sha256}  SHA256SUMS.txt\n",
        encoding="utf-8",
    )
    return {
        "file_count": len(files),
        "total_bytes": sum(item.stat().st_size for item in files),
        "checksum_file": checksum_path.name,
        "checksum_sha256": checksum_sha256,
        "detached_checksum_file": detached_path.name,
    }


def _verify_bundle_checksums(bundle: Path) -> dict[str, Any]:
    checksum_path = bundle / "SHA256SUMS.txt"
    detached_path = bundle / "SHA256SUMS.sha256"
    detached = detached_path.read_text(encoding="utf-8-sig").strip()
    match = re.fullmatch(r"([0-9a-fA-F]{64})  SHA256SUMS\.txt", detached)
    if not match or _sha256(checksum_path) != match.group(1).lower():
        raise RuntimeError("SHA256SUMS.txt 的独立摘要验证失败")

    checked = 0
    for line_number, line in enumerate(
        checksum_path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        if not line:
            continue
        entry = re.fullmatch(r"([0-9a-fA-F]{64})  (.+)", line)
        if not entry:
            raise RuntimeError(f"SHA256SUMS.txt 第 {line_number} 行格式无效")
        target = (bundle / Path(entry.group(2))).resolve()
        try:
            target.relative_to(bundle.resolve())
        except ValueError as exc:
            raise RuntimeError(f"校验路径越界：{entry.group(2)}") from exc
        if not target.is_file():
            raise RuntimeError(f"校验文件缺失：{entry.group(2)}")
        if _sha256(target) != entry.group(1).lower():
            raise RuntimeError(f"校验摘要不匹配：{entry.group(2)}")
        checked += 1
    return {
        "ok": True,
        "verified_files": checked,
        "checksum_sha256": _sha256(checksum_path),
    }


def _shop_mapping() -> list[dict[str, Any]]:
    settings = json.loads(
        (PROJECT_ROOT / "config" / "settings.json").read_text(encoding="utf-8-sig")
    )
    return [
        {
            "id": shop.get("id"),
            "name": shop.get("name"),
            "source_profile": shop.get("edge_profile"),
            "enabled": bool(shop.get("enabled", True)),
        }
        for shop in settings.get("shops", [])
    ]


def _baseline_diff(checksum_path: Path | None) -> dict[str, Any]:
    current: dict[str, str] = {}
    for current_root, directories, files in os.walk(PROJECT_ROOT):
        current_path = Path(current_root)
        directories[:] = [
            name for name in directories if name not in SOURCE_EXCLUDED_DIRECTORIES
        ]
        for name in files:
            item = current_path / name
            if item.suffix.lower() in SOURCE_EXCLUDED_SUFFIXES:
                continue
            relative = "project/" + item.relative_to(PROJECT_ROOT).as_posix()
            current[relative] = _sha256(item)

    baseline: dict[str, str] = {}
    if checksum_path and checksum_path.is_file():
        for line in checksum_path.read_text(encoding="utf-8-sig").splitlines():
            match = re.fullmatch(r"([0-9a-fA-F]{64})  (project/.+)", line)
            if not match:
                continue
            relative = match.group(2)
            parts = Path(relative).parts
            if any(part in SOURCE_EXCLUDED_DIRECTORIES for part in parts):
                continue
            if Path(relative).suffix.lower() in SOURCE_EXCLUDED_SUFFIXES:
                continue
            baseline[relative] = match.group(1).lower()

    added = sorted(set(current) - set(baseline))
    removed = sorted(set(baseline) - set(current))
    modified = sorted(
        path for path in set(current) & set(baseline) if current[path] != baseline[path]
    )
    return {
        "available": bool(baseline),
        "baseline_checksum_file": str(checksum_path.resolve())
        if checksum_path and checksum_path.is_file()
        else "",
        "baseline_checksum_sha256": _sha256(checksum_path)
        if checksum_path and checksum_path.is_file()
        else "",
        "baseline_files": len(baseline),
        "current_files": len(current),
        "unchanged": len(set(current) & set(baseline)) - len(modified),
        "added": added,
        "modified": modified,
        "removed": removed,
        "summary": {
            "added": len(added),
            "modified": len(modified),
            "removed": len(removed),
        },
    }


def _write_version_diff(bundle: Path, difference: dict[str, Any]) -> None:
    lines = [
        "# 版本变更清单",
        "",
        "本清单由当前源码与上一版 `SHA256SUMS.txt` 逐文件比较生成。哈希相同才视为未变化。",
        "",
        f"- 基线文件：{difference['baseline_files']}",
        f"- 当前文件：{difference['current_files']}",
        f"- 新增：{difference['summary']['added']}",
        f"- 修改：{difference['summary']['modified']}",
        f"- 删除：{difference['summary']['removed']}",
        f"- 未变化：{difference['unchanged']}",
        "",
    ]
    if not difference["available"]:
        lines.extend(["未找到可用的上一版校验清单，无法生成文件级差异。", ""])
    else:
        for key, title in (("added", "新增文件"), ("modified", "修改文件"), ("removed", "删除文件")):
            lines.extend([f"## {title}", ""])
            paths = difference[key]
            if paths:
                lines.extend(f"- `{path}`" for path in paths)
            else:
                lines.append("- 无")
            lines.append("")
    (bundle / "版本变更清单.md").write_text("\n".join(lines), encoding="utf-8")


def _copy_bundle_documents(bundle: Path, project_target: Path) -> None:
    document_mapping = {
        "MIGRATION_GUIDE.md": "迁移说明.md",
        "MIGRATION_CHECKLIST.md": "迁移验收清单.md",
        "MIGRATION_ROLLBACK.md": "回滚操作手册.md",
        "MIGRATION_RELEASE_NOTES.md": "本版变化说明.md",
    }
    for source_name, target_name in document_mapping.items():
        shutil.copy2(project_target / "docs" / source_name, bundle / target_name)

    shutil.copy2(
        project_target / "scripts" / "verify_migration_bundle.ps1",
        bundle / "00_校验迁移包.ps1",
    )
    (bundle / "00_校验迁移包.cmd").write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"%~dp000_校验迁移包.ps1\" -BundleRoot \"%~dp0\"\r\n"
        "set \"verify_exit=%ERRORLEVEL%\"\r\n"
        "if not \"%verify_exit%\"==\"0\" echo 校验失败，请勿安装。\r\n"
        "pause\r\n"
        "exit /b %verify_exit%\r\n",
        encoding="utf-8-sig",
    )
    (bundle / "01_安装迁移项目.cmd").write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "call \"%~dp0project\\安装迁移项目.cmd\"\r\n",
        encoding="utf-8-sig",
    )


def _write_content_inventory(bundle: Path, manifest: dict[str, Any]) -> None:
    runtime = dict(manifest["runtime"])
    database = dict(runtime["deployed_database_inventory"])
    lines = [
        "# 迁移包内容清单",
        "",
        f"生成时间：{manifest['created_at']}",
        "",
        "## 分区统计",
        "",
        "| 分区 | 文件数 | 大小（字节） | 说明 |",
        "|---|---:|---:|---|",
    ]
    section_notes = {
        "project": "可部署代码、暂停数据库和历史证据",
        "offline": "Python 安装包与锁定依赖",
        "machine_bound_backup": "原始数据库和浏览器留档，禁止直接部署",
        "diagnostics": "源电脑运行日志",
    }
    for name, inventory in manifest["package_sections"].items():
        lines.append(
            f"| `{name}/` | {inventory['files']} | {inventory['bytes']} | {section_notes.get(name, '')} |"
        )
    lines.extend(
        [
            "",
            "## 数据库一致性快照",
            "",
            f"- 完整性：`{database['integrity']}`",
            f"- 部署副本大小：{database['bytes']} 字节",
            f"- 表数量：{database['table_count']}",
            f"- 全局暂停：`{str(database['global_paused']).lower()}`",
            f"- 运行状态：`{database['runtime_state']}`",
            f"- 原始快照 SHA-256：`{runtime['raw_database_sha256']}`",
            f"- 部署副本 SHA-256：`{runtime['deployed_database_sha256']}`",
            "",
            "### 表记录数",
            "",
            "| 表 | 记录数 |",
            "|---|---:|",
        ]
    )
    lines.extend(f"| `{item['name']}` | {item['rows']} |" for item in database["tables"])
    lines.extend(
        [
            "",
            "## 明确排除项",
            "",
            "- `.venv`、`.pytest_cache`、`__pycache__`：在目标机重建；",
            "- 源 `runtime` 整目录：按数据库、证据、数据文件、日志和机器绑定 Profile 分类复制；",
            "- 源电脑浏览器登录态：只进入 `machine_bound_backup` 留档，不进入目标运行目录；",
            "- 圆通 DPAPI 凭据：不在项目目录内，也不进入迁移包；",
            "- 临时 `.db`、`.pyc`、`.pyo`：不属于部署源码；",
            "- 锁文件、缓存、GPU 缓存和浏览器临时文件：不进入机器绑定留档。",
            "",
            "逐文件路径和 SHA-256 见 `SHA256SUMS.txt`。",
            "",
        ]
    )
    (bundle / "迁移包内容清单.md").write_text("\n".join(lines), encoding="utf-8")


def _estimate_payload_bytes(offline_source: Path | None) -> int:
    total = 0
    for current_root, directories, files in os.walk(PROJECT_ROOT):
        current = Path(current_root)
        directories[:] = [
            name for name in directories if name not in SOURCE_EXCLUDED_DIRECTORIES
        ]
        total += sum(
            (current / name).stat().st_size
            for name in files
            if (current / name).suffix.lower() not in SOURCE_EXCLUDED_SUFFIXES
        )
    source_runtime = PROJECT_ROOT / "runtime"
    for path in (source_runtime / "evidence", source_runtime / "edge_profiles"):
        total += _directory_inventory(path)["bytes"]
    total += 2 * (source_runtime / "rpa.db").stat().st_size
    if offline_source and offline_source.is_dir():
        total += _directory_inventory(offline_source)["bytes"]
    return total


def _create_zip_archive(bundle: Path) -> dict[str, Any]:
    archive = Path(
        shutil.make_archive(
            str(bundle),
            "zip",
            root_dir=str(bundle.parent),
            base_dir=bundle.name,
        )
    )
    with zipfile.ZipFile(archive, "r", allowZip64=True) as handle:
        bad_file = handle.testzip()
        if bad_file:
            raise RuntimeError(f"ZIP 完整性测试失败：{bad_file}")
        entries = len(handle.infolist())
    digest = _sha256(archive)
    digest_path = archive.with_suffix(archive.suffix + ".sha256")
    digest_path.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    return {
        "path": str(archive),
        "bytes": archive.stat().st_size,
        "sha256": digest,
        "sha256_file": str(digest_path),
        "entries": entries,
        "zip_integrity": "ok",
    }


def create_bundle(
    output_root: Path,
    *,
    offline_source: Path | None = None,
    baseline_checksums: Path | None = None,
    create_zip: bool = False,
    verify_after_build: bool = True,
) -> dict[str, Any]:
    if not output_root.is_absolute():
        raise RuntimeError("输出目录必须是绝对路径")
    output_root.mkdir(parents=True, exist_ok=True)

    if offline_source is None:
        candidate = PROJECT_ROOT.parent / "offline"
        offline_source = candidate if candidate.is_dir() else None
    if baseline_checksums is None:
        candidate = PROJECT_ROOT.parent / "SHA256SUMS.txt"
        baseline_checksums = candidate if candidate.is_file() else None

    estimated_payload = _estimate_payload_bytes(offline_source)
    multiplier = 2.2 if create_zip else 1.25
    required_free = int(estimated_payload * multiplier + 2 * 1024**3)
    free = shutil.disk_usage(output_root).free
    if free < required_free:
        raise RuntimeError(
            "输出磁盘空间不足："
            f"估算至少需要 {required_free / 1024**3:.2f} GB，"
            f"当前可用 {free / 1024**3:.2f} GB"
        )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bundle = output_root / f"PDD_RPA_Migration_{stamp}"
    if bundle.exists():
        raise RuntimeError(f"迁移目录已存在，拒绝覆盖：{bundle}")
    bundle.mkdir(parents=True)
    project_target = bundle / "project"
    started = time.monotonic()
    try:
        source_skipped: list[dict[str, str]] = []
        source_copy = _copy_source(project_target, source_skipped)
        if source_skipped:
            raise RuntimeError(
                f"源码复制不完整，拒绝生成迁移包：{len(source_skipped)} 个文件"
            )
        runtime = _copy_runtime(bundle, project_target)
        offline = _prepare_offline_assets(bundle, offline_source)
        _copy_bundle_documents(bundle, project_target)

        difference = _baseline_diff(baseline_checksums)
        _write_version_diff(bundle, difference)

        previous_manifest_path = PROJECT_ROOT.parent / "MIGRATION_MANIFEST.json"
        previous_manifest: dict[str, Any] = {}
        if previous_manifest_path.is_file():
            try:
                previous_manifest = json.loads(
                    previous_manifest_path.read_text(encoding="utf-8-sig")
                )
            except (OSError, ValueError):
                previous_manifest = {}

        package_sections = {
            name: _directory_inventory(bundle / name)
            for name in ("project", "offline", "machine_bound_backup", "diagnostics")
        }
        copy_warnings = [
            *runtime.get("diagnostic_copy_warnings", []),
            *runtime.get("machine_bound_profile_skipped", []),
        ]
        manifest = {
            "format_version": BUNDLE_FORMAT_VERSION,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source_project_root": str(PROJECT_ROOT.resolve()),
            "bundle_root": str(bundle),
            "source_runtime_or_business_config_modified_by_builder": False,
            "source_runtime_stopped_by_builder": False,
            "business_action_performed": False,
            "deployable_project_safety": {
                "global_paused": True,
                "runtime_state": "STOPPED",
                "live_cutover_requires_manual_approval": True,
                "source_execution_mode_preserved": True,
            },
            "baseline": {
                "bundle_created_at": str(previous_manifest.get("created_at") or ""),
                "bundle_root": str(previous_manifest.get("bundle_root") or ""),
                "manifest_path": str(previous_manifest_path)
                if previous_manifest_path.is_file()
                else "",
                "manifest_sha256": _sha256(previous_manifest_path)
                if previous_manifest_path.is_file()
                else "",
            },
            "environment": {
                "os": platform.platform(),
                "python": sys.version,
                "python_executable": sys.executable,
                "edge": _edge_info(),
                "display": _display_metrics(),
                "applications": _source_application_paths(),
            },
            "configuration": _configuration_inventory(),
            "shops": _shop_mapping(),
            "source_copy": source_copy,
            "version_difference": difference,
            "runtime": runtime,
            "offline": offline,
            "package_sections": package_sections,
            "copy_warning_summary": {
                "total": len(copy_warnings),
                "diagnostic_logs": len(runtime.get("diagnostic_copy_warnings", [])),
                "machine_bound_profiles": len(
                    runtime.get("machine_bound_profile_skipped", [])
                ),
                "deployable_source_or_evidence_skipped": 0,
            },
            "copy_warnings": copy_warnings,
            "integrity": {
                "algorithm": "SHA-256",
                "file": "SHA256SUMS.txt",
                "detached_hash_file": "SHA256SUMS.sha256",
            },
            "estimated_payload_bytes_before_build": estimated_payload,
            "elapsed_seconds_before_hashing": round(time.monotonic() - started, 2),
            "manual_actions_required_on_target": [
                "先运行 00_校验迁移包.cmd，全部 SHA-256 匹配后再安装",
                "安装并登录旺店通、企业微信和微信",
                "创建并登录 11 家独立拼多多 Edge Profile，显示名称必须是完整店铺名",
                "在千牛独立 Edge 中登录示例天猫旗舰店并完成只读扫描",
                "在新 Windows 用户下重新配置圆通 DPAPI 凭据并验证登录",
                "在钉钉独立窗口重新登录并核验示例自营仓、示例团队分销两个表",
                "完成旺店通、消息群、圆通、钉钉、千牛和 11 店只读预检",
                "填写本机 migration_validation.json，不沿用旧报告",
                "确保源电脑与新电脑不同时正式运行",
            ],
        }
        manifest_path = bundle / "MIGRATION_MANIFEST.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _write_content_inventory(bundle, manifest)

        package_files = [item for item in bundle.rglob("*") if item.is_file()]
        package_bytes = sum(item.stat().st_size for item in package_files)
        (bundle / "封装完成.txt").write_text(
            "\n".join(
                [
                    "拼多多/千牛售后 RPA 迁移包已生成。",
                    f"生成时间：{manifest['created_at']}",
                    f"项目目录：{project_target}",
                    f"校验前文件数：{len(package_files)}",
                    f"校验前总大小：{package_bytes / 1024**3:.2f} GB",
                    f"相对上一版：新增 {difference['summary']['added']}，修改 {difference['summary']['modified']}，删除 {difference['summary']['removed']}。",
                    f"机器绑定/诊断复制告警：{len(copy_warnings)}（不影响部署源码与业务证据完整性）。",
                    "第一步：00_校验迁移包.cmd",
                    "第二步：01_安装迁移项目.cmd",
                    "新电脑数据库默认全局暂停；全部登录、只读预检和签字完成前严禁解除暂停。",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        checksum = _write_checksums(bundle)
        verification = (
            _verify_bundle_checksums(bundle)
            if verify_after_build
            else {"ok": None, "verified_files": 0, "skipped": True}
        )
        archive = _create_zip_archive(bundle) if create_zip else None
        return {
            "ok": True,
            "bundle": str(bundle),
            "checksum": checksum,
            "verification": verification,
            "archive": archive,
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
    except Exception as exc:
        (bundle / "_封装未完成.txt").write_text(
            f"迁移包封装失败，不可用于部署。\n错误：{exc}\n",
            encoding="utf-8",
        )
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", default=str(PROJECT_ROOT.parent.parent))
    parser.add_argument("--offline-source", default="")
    parser.add_argument("--baseline-checksums", default="")
    parser.add_argument("--zip", action="store_true", dest="create_zip")
    parser.add_argument("--skip-post-build-verify", action="store_true")
    args = parser.parse_args()
    result = create_bundle(
        Path(args.output_root).expanduser().resolve(),
        offline_source=Path(args.offline_source).expanduser().resolve()
        if args.offline_source
        else None,
        baseline_checksums=Path(args.baseline_checksums).expanduser().resolve()
        if args.baseline_checksums
        else None,
        create_zip=bool(args.create_zip),
        verify_after_build=not args.skip_post_build_verify,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
