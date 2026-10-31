from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import ExecutionMode, Shop


@dataclass(frozen=True, slots=True)
class AppConfig:
    project_root: Path
    database_path: Path
    evidence_dir: Path
    execution_mode: ExecutionMode
    live_actions_enabled: bool
    shops: tuple[Shop, ...]
    raw: dict[str, Any]


def load_config(path: str | Path | None = None) -> AppConfig:
    project_root = Path(__file__).resolve().parent.parent
    config_path = Path(path) if path else project_root / "config" / "settings.json"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    runtime = data.get("runtime", {})
    database_path = project_root / runtime.get("database_path", "runtime/rpa.db")
    evidence_dir = project_root / runtime.get("evidence_dir", "runtime/evidence")
    shops = tuple(Shop(**shop) for shop in data.get("shops", []))
    return AppConfig(
        project_root=project_root,
        database_path=database_path,
        evidence_dir=evidence_dir,
        execution_mode=ExecutionMode(data.get("execution_mode", "observe")),
        live_actions_enabled=bool(data.get("live_actions_enabled", False)),
        shops=shops,
        raw=data,
    )

