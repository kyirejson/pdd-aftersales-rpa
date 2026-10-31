"""Read-only readiness report for a migrated PDD RPA computer."""

from __future__ import annotations

import ctypes
import importlib
import json
import os
import platform
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"
DATABASE_PATH = PROJECT_ROOT / "runtime" / "rpa.db"
REPORT_PATH = PROJECT_ROOT / "runtime" / "migration_preflight.json"
VALIDATION_PATH = PROJECT_ROOT / "runtime" / "migration_validation.json"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _edge_path() -> Path | None:
    candidates = (
        Path(os.environ.get("PROGRAMFILES(X86)", ""))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", ""))
        / "Microsoft/Edge/Application/msedge.exe",
    )
    return next((item for item in candidates if item.is_file()), None)


def _edge_profile_cache(user_data_dir: Path) -> dict[str, dict[str, Any]]:
    local_state = user_data_dir / "Local State"
    if not local_state.is_file():
        return {}
    try:
        payload = _load_json(local_state)
    except (OSError, ValueError):
        return {}
    return dict(payload.get("profile", {}).get("info_cache", {}) or {})


def _database_state() -> dict[str, Any]:
    if not DATABASE_PATH.is_file():
        return {"ok": False, "error": "runtime/rpa.db 不存在"}
    connection = sqlite3.connect(f"file:{DATABASE_PATH.as_posix()}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        settings = {
            key: value
            for key, value in connection.execute(
                "SELECT key, value FROM system_settings "
                "WHERE key IN ('global_paused', 'runtime_controller_status')"
            )
        }
        paused = str(settings.get("global_paused") or "").lower() == "true"
        runtime_state = ""
        try:
            runtime_state = str(
                json.loads(settings.get("runtime_controller_status") or "{}").get("state")
                or ""
            )
        except (TypeError, ValueError):
            pass
        return {
            "ok": integrity == "ok" and paused and runtime_state == "STOPPED",
            "integrity": integrity,
            "global_paused": paused,
            "runtime_state": runtime_state,
        }
    finally:
        connection.close()


def _module_checks() -> list[dict[str, Any]]:
    modules = {
        "fastapi": "FastAPI",
        "uvicorn": "Uvicorn",
        "pywinauto": "UIA",
        "win32api": "pywin32",
        "PIL": "Pillow",
        "playwright.sync_api": "Playwright",
        "httpx2": "httpx2",
    }
    results = []
    for module, label in modules.items():
        try:
            importlib.import_module(module)
            results.append({"name": label, "ok": True})
        except Exception as exc:
            results.append({"name": label, "ok": False, "error": str(exc)})
    return results


def _common_application_candidates() -> dict[str, tuple[Path, ...]]:
    program_files = Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
    program_files_x86 = Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    return {
        "旺店通ERP旗舰版": (
            Path("D:/旺店通/旺店通ERP旗舰版/旺店通ERP旗舰版.exe"),
            Path("D:/旺店通ERP旗舰版/旺店通ERP旗舰版.exe"),
            program_files / "旺店通/旺店通ERP旗舰版.exe",
            program_files_x86 / "旺店通/旺店通ERP旗舰版.exe",
        ),
        "企业微信": (
            Path("D:/WXWork/WXWork.exe"),
            Path("D:/Program Files/WXWork/WXWork.exe"),
            program_files / "WXWork/WXWork.exe",
            program_files_x86 / "WXWork/WXWork.exe",
        ),
        "微信": (
            Path("D:/WX/Weixin/Weixin.exe"),
            program_files / "Tencent/WeChat/WeChat.exe",
            program_files_x86 / "Tencent/WeChat/WeChat.exe",
            local / "Tencent/WeChat/WeChat.exe",
            local / "Tencent/xwechat/xwechat.exe",
        ),
    }


def _display_metrics() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    user32 = ctypes.windll.user32
    width = int(user32.GetSystemMetrics(0))
    height = int(user32.GetSystemMetrics(1))
    dpi = 96
    try:
        dpi = int(user32.GetDpiForSystem())
    except Exception:
        pass
    return {
        "width": width,
        "height": height,
        "dpi": dpi,
        "scale_percent": round(dpi / 96 * 100),
    }


def _resolve_project_path(value: str) -> Path:
    path = Path(str(value or "").strip())
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _qianniu_validation_ok(validation: dict[str, Any], *, enabled: bool) -> bool:
    if not enabled:
        return True
    result = dict(validation.get("qianniu") or {})
    return bool(
        result.get("ok")
        and result.get("shop_name_matches")
        and result.get("list_url_matches")
        and result.get("readonly_scan_ok")
        and result.get("wdt_readonly_query_ok")
        and not result.get("business_write_performed")
    )


def _dingtalk_targets_validation(
    settings: dict[str, Any], validation: dict[str, Any]
) -> dict[str, Any]:
    configured = dict(
        dict(settings.get("dingtalk_interception_register") or {}).get("targets") or {}
    )
    validation_section = dict(validation.get("dingtalk") or {})
    recorded = dict(validation_section.get("targets") or {})
    results: list[dict[str, Any]] = []
    for key, target in configured.items():
        item = dict(recorded.get(key) or {})
        # A one-target v1 validation report can still be read, but it cannot
        # satisfy the current two-target production configuration.
        if not recorded and len(configured) == 1:
            item = validation_section
        ok = bool(
            item.get("ok")
            and item.get("document_matches", True)
            and item.get("sheet_matches", True)
            and item.get("layout_checked", True)
            and not item.get("write_performed")
        )
        results.append(
            {
                "key": key,
                "warehouse_aliases": list(dict(target or {}).get("warehouse_aliases") or []),
                "sheet_name": str(dict(target or {}).get("sheet_name") or ""),
                "ok": ok,
            }
        )
    return {
        "configured_count": len(configured),
        "passed_count": sum(1 for item in results if item["ok"]),
        "all_ok": all(item["ok"] for item in results) if results else True,
        "targets": results,
    }


def build_report() -> dict[str, Any]:
    settings = _load_json(SETTINGS_PATH)
    try:
        validation = _load_json(VALIDATION_PATH)
    except (OSError, ValueError):
        validation = {}
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    edge_user_data = Path(local_app_data) / "Microsoft/Edge/User Data"
    cache = _edge_profile_cache(edge_user_data)

    shops = []
    for shop in settings.get("shops", []):
        directory = str(shop.get("edge_profile") or "").strip()
        actual_name = str(dict(cache.get(directory) or {}).get("name") or "").strip()
        expected_name = str(shop.get("name") or "").strip()
        shops.append(
            {
                "shop": expected_name,
                "profile_directory": directory,
                "profile_exists": directory in cache,
                "profile_name": actual_name,
                "name_matches": actual_name == expected_name,
                "ok": directory in cache and actual_name == expected_name,
            }
        )

    yto = dict(settings.get("yto_service_chat") or {})
    yto_directory = str(yto.get("source_profile_directory") or "").strip()
    yto_exists = yto_directory in cache
    yto_playwright_root = Path(str(yto.get("playwright_user_data_dir") or ""))
    if not yto_playwright_root.is_absolute():
        yto_playwright_root = PROJECT_ROOT / yto_playwright_root
    yto_isolated_ready = bool(
        (yto_playwright_root / ".yto_profile_ready").is_file()
        and (yto_playwright_root / yto_directory).is_dir()
    )

    qianniu = dict(settings.get("qianniu_shop_browser") or {})
    qianniu_enabled = bool(qianniu.get("enabled"))
    qianniu_user_data = _resolve_project_path(
        str(qianniu.get("user_data_dir") or "runtime/edge_profiles/qianniu_edge")
    )
    qianniu_profile_ready = bool(
        (qianniu_user_data / "Local State").is_file()
        and any(
            item.is_dir()
            for item in qianniu_user_data.glob("*")
            if item.name == "Default" or item.name.startswith("Profile ")
        )
    )
    qianniu_validation_ok = _qianniu_validation_ok(
        validation, enabled=qianniu_enabled
    )
    dingtalk_targets = _dingtalk_targets_validation(settings, validation)

    applications = []
    application_login_keys = {
        "旺店通ERP旗舰版": "wdt",
        "企业微信": "wecom",
        "微信": "wechat",
    }
    for name, candidates in _common_application_candidates().items():
        found = next((item for item in candidates if item.is_file()), None)
        applications.append(
            {
                "name": name,
                "installed": found is not None,
                "path": str(found) if found else "",
                "manual_login_required": not bool(
                    dict(validation.get("accounts") or {}).get(
                        application_login_keys.get(name, "")
                    )
                ),
            }
        )

    edge = _edge_path()
    modules = _module_checks()
    database = _database_state()
    required_accounts = ["pdd_11_shops", "wdt", "wecom", "wechat", "yto", "dingtalk"]
    if qianniu_enabled:
        required_accounts.append("qianniu")
    validation_accounts = dict(validation.get("accounts") or {})
    shop_validation = dict(validation.get("shops") or {})
    pdd_readonly_preflighted = bool(
        shop_validation.get("passed") == len(shops)
        and shop_validation.get("total") == len(shops)
        and shop_validation.get("profile_names_match")
        and shop_validation.get("live_windows_match")
        and shop_validation.get("list_urls_match")
        and shop_validation.get("readonly_detail_checks_passed")
    )
    message_validation = dict(validation.get("message_groups") or {})
    desktop_validation = dict(validation.get("desktop") or {})
    desktop_environment_checked = bool(
        desktop_validation.get("resolution_and_scale_checked")
        and desktop_validation.get("no_blocking_overlays")
        and desktop_validation.get("unlocked_session_available")
    )
    cutover_validation = dict(validation.get("cutover") or {})
    cutover_confirmed = bool(
        cutover_validation.get("source_stopped")
        and cutover_validation.get("single_executor_confirmed")
        and str(cutover_validation.get("approved_by") or "").strip()
        and str(cutover_validation.get("approved_at") or "").strip()
    )

    blocking_checks = {
        "windows_64_bit": os.name == "nt" and platform.machine().endswith("64"),
        "python_3_13": sys.version_info[:2] == (3, 13),
        "dependencies": all(item["ok"] for item in modules),
        "database_safe": bool(database.get("ok")),
        "edge_installed": edge is not None,
    }
    manual_checks = {
        "validation_business_action_free": not bool(
            validation.get("business_action_performed")
        ),
        "all_shop_profiles_mapped": all(item["ok"] for item in shops),
        "pdd_readonly_preflighted": pdd_readonly_preflighted,
        "yto_source_profile_exists": yto_exists or yto_isolated_ready,
        "qianniu_profile_ready": not qianniu_enabled or qianniu_profile_ready,
        "external_apps_installed": all(item["installed"] for item in applications),
        "all_accounts_logged_in": all(
            bool(validation_accounts.get(name)) for name in required_accounts
        ),
        "qianniu_readonly_preflighted": qianniu_validation_ok,
        "dingtalk_targets_preflighted": bool(dingtalk_targets["all_ok"]),
        "message_groups_preflighted": bool(
            message_validation.get("passed") == message_validation.get("total")
            and message_validation.get("total")
            and not message_validation.get("sent")
        ),
        "wdt_query_preflighted": bool(
            dict(validation.get("wdt_query") or {}).get("ok")
            and dict(validation.get("wdt_query") or {}).get("full_component_audit_ok")
            and dict(validation.get("wdt_query") or {}).get("result_waybill_verified")
            and not dict(validation.get("wdt_query") or {}).get("business_write_performed")
        ),
        "desktop_environment_checked": desktop_environment_checked,
        "source_stopped_and_cutover_approved": cutover_confirmed,
    }
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "project_root": str(PROJECT_ROOT),
        "business_action_performed": False,
        "blocking_checks": blocking_checks,
        "manual_checks": manual_checks,
        "ready_for_console": all(blocking_checks.values()),
        "ready_for_live_cutover": all(blocking_checks.values())
        and all(manual_checks.values()),
        "python": sys.version,
        "os": platform.platform(),
        "display": _display_metrics(),
        "edge": {"installed": edge is not None, "path": str(edge or "")},
        "database": database,
        "modules": modules,
        "shops": shops,
        "yto": {
            "source_profile_directory": yto_directory,
            "profile_exists": yto_exists,
            "isolated_profile_ready": yto_isolated_ready,
            "manual_login_required": not bool(
                dict(validation.get("accounts") or {}).get("yto")
            ),
        },
        "qianniu": {
            "enabled": qianniu_enabled,
            "shop_name": str(qianniu.get("shop_name") or ""),
            "user_data_dir": str(qianniu_user_data),
            "profile_ready": qianniu_profile_ready,
            "manual_login_required": qianniu_enabled
            and not bool(validation_accounts.get("qianniu")),
            "readonly_validation_ok": qianniu_validation_ok,
        },
        "dingtalk": {
            "user_data_dir": str(
                settings.get("dingtalk_interception_register", {}).get(
                    "playwright_user_data_dir", ""
                )
            ),
            "manual_login_required": not bool(
                dict(validation.get("accounts") or {}).get("dingtalk")
            ),
            "target_validation": dingtalk_targets,
        },
        "applications": applications,
        "validation": validation,
        "cutover_warning": "源电脑与新电脑禁止同时开启正式自动执行。",
    }


def main() -> int:
    report = build_report()
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ready_for_console"]:
        return 1
    if not report["ready_for_live_cutover"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
