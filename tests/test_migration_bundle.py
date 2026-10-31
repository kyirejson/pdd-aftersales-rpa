from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    path = PROJECT_ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_settings_table(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE system_settings ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO system_settings(key, value, updated_at) VALUES(?, ?, ?)",
            (
                "runtime_controller_status",
                json.dumps({"state": "RUNNING", "current_task": 123}),
                "before",
            ),
        )
        connection.execute(
            "INSERT INTO system_settings(key, value, updated_at) VALUES(?, ?, ?)",
            ("global_paused", "false", "before"),
        )
        connection.commit()
    finally:
        connection.close()


def test_force_safe_snapshot_sets_both_database_gates(tmp_path: Path) -> None:
    builder = _load_script("create_migration_bundle.py")
    database = tmp_path / "rpa.db"
    _create_settings_table(database)

    builder._force_safe_snapshot(database)

    connection = sqlite3.connect(database)
    try:
        settings = dict(connection.execute("SELECT key, value FROM system_settings"))
    finally:
        connection.close()
    status = json.loads(settings["runtime_controller_status"])
    assert settings["global_paused"] == "true"
    assert status["state"] == "STOPPED"
    assert status["phase"] == "migration_bundle_ready"
    assert status["current_task"] is None


def test_bundle_checksums_cover_payload_and_detect_tampering(tmp_path: Path) -> None:
    builder = _load_script("create_migration_bundle.py")
    payload = tmp_path / "project" / "示例.txt"
    payload.parent.mkdir()
    payload.write_text("完整\n", encoding="utf-8")

    summary = builder._write_checksums(tmp_path)
    verified = builder._verify_bundle_checksums(tmp_path)

    assert summary["file_count"] == 1
    assert verified["ok"] is True
    assert verified["verified_files"] == 1

    payload.write_text("已修改\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="摘要不匹配"):
        builder._verify_bundle_checksums(tmp_path)


def test_baseline_diff_reports_added_modified_and_removed(
    tmp_path: Path, monkeypatch
) -> None:
    builder = _load_script("create_migration_bundle.py")
    project = tmp_path / "project"
    (project / "pdd_rpa").mkdir(parents=True)
    unchanged = project / "pdd_rpa" / "unchanged.py"
    modified = project / "pdd_rpa" / "modified.py"
    added = project / "pdd_rpa" / "added.py"
    unchanged.write_text("same\n", encoding="utf-8")
    modified.write_text("new\n", encoding="utf-8")
    added.write_text("added\n", encoding="utf-8")
    (project / "pdd_rpa" / "ignored.db").write_bytes(b"ignored")

    old_modified = tmp_path / "old_modified.py"
    old_removed = tmp_path / "old_removed.py"
    old_modified.write_text("old\n", encoding="utf-8")
    old_removed.write_text("removed\n", encoding="utf-8")
    checksums = tmp_path / "SHA256SUMS.txt"
    checksums.write_text(
        "\n".join(
            [
                f"{_sha256(unchanged)}  project/pdd_rpa/unchanged.py",
                f"{_sha256(old_modified)}  project/pdd_rpa/modified.py",
                f"{_sha256(old_removed)}  project/pdd_rpa/removed.py",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(builder, "PROJECT_ROOT", project)

    result = builder._baseline_diff(checksums)

    assert result["added"] == ["project/pdd_rpa/added.py"]
    assert result["modified"] == ["project/pdd_rpa/modified.py"]
    assert result["removed"] == ["project/pdd_rpa/removed.py"]
    assert result["unchanged"] == 1


def test_migration_preflight_requires_qianniu_and_each_dingtalk_target() -> None:
    preflight = _load_script("migration_preflight.py")
    settings = {
        "dingtalk_interception_register": {
            "targets": {
                "junfu": {"warehouse_aliases": ["示例自营仓"], "sheet_name": "2026"},
                "siku_distribution": {
                    "warehouse_aliases": ["示例供应链分销"],
                    "sheet_name": "2026登记",
                },
            }
        }
    }
    validation = {
        "qianniu": {
            "ok": True,
            "shop_name_matches": True,
            "list_url_matches": True,
            "readonly_scan_ok": True,
            "wdt_readonly_query_ok": True,
            "business_write_performed": False,
        },
        "dingtalk": {
            "targets": {
                "junfu": {
                    "ok": True,
                    "document_matches": True,
                    "sheet_matches": True,
                    "layout_checked": True,
                    "write_performed": False,
                }
            }
        },
    }

    assert preflight._qianniu_validation_ok(validation, enabled=True)
    result = preflight._dingtalk_targets_validation(settings, validation)
    assert result["configured_count"] == 2
    assert result["passed_count"] == 1
    assert not result["all_ok"]


def test_prepare_rebinds_absolute_qianniu_profile_to_target_project(
    tmp_path: Path, monkeypatch
) -> None:
    prepare = _load_script("prepare_migrated_machine.py")
    project = tmp_path / "project"
    local = tmp_path / "local"
    edge = local / "Microsoft" / "Edge" / "User Data"
    edge.mkdir(parents=True)
    (edge / "Local State").write_text(
        json.dumps({"profile": {"info_cache": {}}}), encoding="utf-8"
    )
    monkeypatch.setattr(prepare, "PROJECT_ROOT", project)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    settings = {
        "shops": [],
        "yto_service_chat": {},
        "qianniu_shop_browser": {"user_data_dir": "C:/old/qianniu"},
    }

    result = prepare._rebind_settings(settings)

    assert settings["qianniu_shop_browser"]["user_data_dir"] == (
        "runtime/edge_profiles/qianniu_edge"
    )
    assert Path(result["qianniu_user_data_dir"]) == (
        project / "runtime" / "edge_profiles" / "qianniu_edge"
    ).resolve()
