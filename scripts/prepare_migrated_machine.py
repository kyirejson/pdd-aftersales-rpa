"""Prepare a copied PDD RPA project for a different Windows computer.

This utility only edits the copied project.  It never opens business pages,
sends messages, or starts a production run.  The migrated database is forced
into STOPPED + globally-paused state until the operator completes the manual
login and UIA preflight checklist.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"
DATABASE_PATH = PROJECT_ROOT / "runtime" / "rpa.db"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _edge_user_data_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("当前 Windows 用户缺少 LOCALAPPDATA，无法定位 Edge 配置")
    return Path(local_app_data) / "Microsoft" / "Edge" / "User Data"


def _edge_profile_names(user_data_dir: Path) -> dict[str, str]:
    """Return ``display name -> profile directory`` for exact unique names."""

    local_state = user_data_dir / "Local State"
    if not local_state.is_file():
        return {}
    try:
        payload = _load_json(local_state)
    except (OSError, ValueError):
        return {}
    info_cache = dict(payload.get("profile", {}).get("info_cache", {}) or {})
    by_name: dict[str, list[str]] = {}
    for directory, details in info_cache.items():
        name = str((details or {}).get("name") or "").strip()
        if name:
            by_name.setdefault(name, []).append(str(directory).strip())
    return {
        name: directories[0]
        for name, directories in by_name.items()
        if len(directories) == 1
    }


def _project_path(value: str, default_relative: str) -> Path:
    configured = Path(str(value or default_relative).strip())
    if configured.is_absolute():
        return configured
    return PROJECT_ROOT / configured


def _rebind_settings(settings: dict[str, Any]) -> dict[str, Any]:
    edge_root = _edge_user_data_dir()
    profile_names = _edge_profile_names(edge_root)
    mapped_shops: list[dict[str, str]] = []
    missing_shops: list[str] = []

    for shop in settings.get("shops", []):
        shop_name = str(shop.get("name") or "").strip()
        mapped = profile_names.get(shop_name)
        if mapped:
            old = str(shop.get("edge_profile") or "")
            shop["edge_profile"] = mapped
            mapped_shops.append({"shop": shop_name, "old": old, "new": mapped})
        else:
            missing_shops.append(shop_name)

    yto = dict(settings.get("yto_service_chat") or {})
    yto["source_edge_user_data_dir"] = edge_root.as_posix()
    yto_display_name = str(yto.get("profile_display_name") or "圆通客户管家").strip()
    detected_yto = profile_names.get(yto_display_name)
    if detected_yto:
        yto["source_profile_directory"] = detected_yto
        yto["edge_profile"] = detected_yto
    settings["yto_service_chat"] = yto

    qianniu = dict(settings.get("qianniu_shop_browser") or {})
    qianniu_user_data = Path(
        str(qianniu.get("user_data_dir") or "runtime/edge_profiles/qianniu_edge")
    )
    if qianniu_user_data.is_absolute():
        qianniu["user_data_dir"] = "runtime/edge_profiles/qianniu_edge"
    else:
        qianniu["user_data_dir"] = qianniu_user_data.as_posix()
    settings["qianniu_shop_browser"] = qianniu
    qianniu_target = _project_path(
        str(qianniu.get("user_data_dir") or ""),
        "runtime/edge_profiles/qianniu_edge",
    )

    return {
        "edge_user_data_dir": str(edge_root),
        "mapped_shops": mapped_shops,
        "missing_shops": missing_shops,
        "yto_profile_display_name": yto_display_name,
        "yto_profile_directory": detected_yto or "",
        "qianniu_user_data_dir": str(qianniu_target.resolve()),
    }


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _path_replacements(source_root: str, target_root: str) -> list[tuple[str, str]]:
    source = str(source_root or "").rstrip("\\/")
    target = str(target_root).rstrip("\\/")
    if not source or source.casefold() == target.casefold():
        return []
    pairs = [
        (source, target),
        (source.replace("\\", "/"), target.replace("\\", "/")),
        (source.replace("\\", "\\\\"), target.replace("\\", "\\\\")),
    ]
    unique: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for old, new in pairs:
        if old and (old, new) not in seen:
            seen.add((old, new))
            unique.append((old, new))
    return unique


def _prepare_database(
    database_path: Path,
    *,
    source_root: str,
    edge_root: Path,
    yto_profile_directory: str,
) -> dict[str, Any]:
    if not database_path.is_file():
        raise RuntimeError(f"迁移数据库不存在：{database_path}")

    backup_dir = database_path.parent / "migration_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"rpa.before_machine_rewrite.{stamp}.db"
    source_connection = sqlite3.connect(database_path)
    backup_connection = sqlite3.connect(backup_path)
    try:
        source_connection.backup(backup_connection)
    finally:
        backup_connection.close()
        source_connection.close()

    target_root = str(PROJECT_ROOT.resolve())
    replacements = _path_replacements(source_root, target_root)
    updated_cells = 0
    connection = sqlite3.connect(database_path)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"迁移数据库完整性检查失败：{integrity}")

        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            columns = connection.execute(
                f"PRAGMA table_info({_quote_identifier(table)})"
            ).fetchall()
            for _cid, column, declared_type, *_rest in columns:
                if "TEXT" not in str(declared_type or "").upper():
                    continue
                table_sql = _quote_identifier(table)
                column_sql = _quote_identifier(column)
                for old, new in replacements:
                    cursor = connection.execute(
                        f"UPDATE {table_sql} "
                        f"SET {column_sql}=REPLACE({column_sql}, ?, ?) "
                        f"WHERE INSTR({column_sql}, ?) > 0",
                        (old, new, old),
                    )
                    updated_cells += max(0, int(cursor.rowcount or 0))

        now = datetime.now().isoformat(timespec="seconds")
        connection.execute(
            "INSERT INTO system_settings(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            ("global_paused", "true", now),
        )

        runtime_row = connection.execute(
            "SELECT value FROM system_settings WHERE key='runtime_controller_status'"
        ).fetchone()
        runtime_status: dict[str, Any] = {}
        if runtime_row:
            try:
                runtime_status = dict(json.loads(runtime_row[0]) or {})
            except (TypeError, ValueError):
                runtime_status = {}
        runtime_status.update(
            {
                "state": "STOPPED",
                "phase": "migration_preflight",
                "current_task": None,
                "next_sync_at": "",
                "recovery_pending": False,
                "recovery_active": False,
                "recovery_attempts": 0,
                "recovery_of": "",
                "last_error": "",
                "stop_requested_at": now,
            }
        )
        connection.execute(
            "INSERT INTO system_settings(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (
                "runtime_controller_status",
                json.dumps(runtime_status, ensure_ascii=False),
                now,
            ),
        )

        connector_row = connection.execute(
            "SELECT value FROM system_settings WHERE key='desktop_message_connector_config'"
        ).fetchone()
        if connector_row:
            try:
                connector = dict(json.loads(connector_row[0]) or {})
            except (TypeError, ValueError):
                connector = {}
            yto = dict(dict(connector.get("platforms") or {}).get("yto") or {})
            yto["source_edge_user_data_dir"] = edge_root.as_posix()
            if yto_profile_directory:
                yto["source_profile_directory"] = yto_profile_directory
                yto["edge_profile"] = yto_profile_directory
            platforms = dict(connector.get("platforms") or {})
            platforms["yto"] = yto
            connector["platforms"] = platforms
            connection.execute(
                "UPDATE system_settings SET value=?, updated_at=? WHERE key=?",
                (
                    json.dumps(connector, ensure_ascii=False),
                    now,
                    "desktop_message_connector_config",
                ),
            )

        migration_record = {
            "prepared_at": now,
            "source_root": source_root,
            "target_root": target_root,
            "global_paused": True,
        }
        connection.execute(
            "INSERT INTO system_settings(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (
                "migration_target_prepared",
                json.dumps(migration_record, ensure_ascii=False),
                now,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return {
        "backup": str(backup_path),
        "updated_path_cells": updated_cells,
        "global_paused": True,
        "runtime_state": "STOPPED",
    }


def prepare(source_root: str) -> dict[str, Any]:
    settings = _load_json(SETTINGS_PATH)
    mapping = _rebind_settings(settings)
    _write_json_atomic(SETTINGS_PATH, settings)

    for directory in (
        PROJECT_ROOT / "runtime" / "edge_profiles" / "dingtalk_playwright",
        PROJECT_ROOT / "runtime" / "edge_profiles" / "yto_playwright",
        Path(mapping["qianniu_user_data_dir"]),
    ):
        directory.mkdir(parents=True, exist_ok=True)

    database = _prepare_database(
        DATABASE_PATH,
        source_root=source_root,
        edge_root=Path(mapping["edge_user_data_dir"]),
        yto_profile_directory=str(mapping["yto_profile_directory"]),
    )
    report = {
        "ok": True,
        "project_root": str(PROJECT_ROOT),
        "source_root": source_root,
        "settings": mapping,
        "database": database,
        "business_action_performed": False,
    }
    _write_json_atomic(PROJECT_ROOT / "runtime" / "migration_machine_ready.json", report)
    return report


def _source_root_from_manifest() -> str:
    manifest = PROJECT_ROOT.parent / "MIGRATION_MANIFEST.json"
    if not manifest.is_file():
        return ""
    try:
        return str(_load_json(manifest).get("source_project_root") or "")
    except (OSError, ValueError):
        return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", default="")
    args = parser.parse_args()
    source_root = str(args.source_root or _source_root_from_manifest()).strip()
    result = prepare(source_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
