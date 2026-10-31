"""Assign the configured shop names to already-created Microsoft Edge profiles.

This utility only changes Edge's profile display-name fields. It does not read
or modify cookies, saved passwords, browsing history, or site storage. Edge
must be fully stopped so it cannot overwrite Local State while names change.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".profile-name.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)


def _edge_is_running() -> bool:
    try:
        import subprocess

        result = subprocess.run(
            ["tasklist.exe", "/FI", "IMAGENAME eq msedge.exe", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return '"msedge.exe"' in result.stdout.lower()
    except OSError:
        return False


def configure() -> dict[str, Any]:
    if _edge_is_running():
        raise RuntimeError("Microsoft Edge 仍在运行；请先关闭 Edge 再修改 Profile 名称")

    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("当前 Windows 用户缺少 LOCALAPPDATA")
    user_data = Path(local_app_data) / "Microsoft" / "Edge" / "User Data"
    local_state_path = user_data / "Local State"
    if not local_state_path.is_file():
        raise RuntimeError(f"Edge Local State 不存在：{local_state_path}")

    settings = _load_json(SETTINGS_PATH)
    shops = [item for item in settings.get("shops", []) if item.get("enabled", True)]
    mapping = {
        str(item.get("edge_profile") or "").strip(): str(item.get("name") or "").strip()
        for item in shops
    }
    if len(mapping) != len(shops) or not mapping or any(not key or not value for key, value in mapping.items()):
        raise RuntimeError("店铺 Profile 目录或显示名称为空/重复")

    local_state = _load_json(local_state_path)
    info_cache = local_state.setdefault("profile", {}).setdefault("info_cache", {})
    missing = [directory for directory in mapping if directory not in info_cache]
    if missing:
        raise RuntimeError(f"Edge Profile 尚未完整创建：{missing}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = PROJECT_ROOT / "runtime" / "migration_backups" / f"edge_profile_names.{stamp}"
    backup_root.mkdir(parents=True, exist_ok=False)
    shutil.copy2(local_state_path, backup_root / "Local State")

    results: list[dict[str, str]] = []
    for directory, shop_name in mapping.items():
        profile_dir = user_data / directory
        preferences_path = profile_dir / "Preferences"
        if not preferences_path.is_file():
            raise RuntimeError(f"Edge Profile Preferences 不存在：{preferences_path}")
        profile_backup = backup_root / directory
        profile_backup.mkdir(parents=True, exist_ok=True)
        shutil.copy2(preferences_path, profile_backup / "Preferences")

        preferences = _load_json(preferences_path)
        profile = preferences.setdefault("profile", {})
        old_preferences_name = str(profile.get("name") or "")
        profile["name"] = shop_name
        _write_json_atomic(preferences_path, preferences)

        info = dict(info_cache[directory] or {})
        old_cache_name = str(info.get("name") or "")
        info["name"] = shop_name
        if "shortcut_name" in info:
            info["shortcut_name"] = shop_name
        info_cache[directory] = info
        results.append(
            {
                "directory": directory,
                "shop": shop_name,
                "old_preferences_name": old_preferences_name,
                "old_cache_name": old_cache_name,
            }
        )

    _write_json_atomic(local_state_path, local_state)
    return {
        "ok": True,
        "user_data_dir": str(user_data),
        "backup": str(backup_root),
        "profiles": results,
        "cookies_read": False,
        "credentials_read": False,
    }


def main() -> int:
    result = configure()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
