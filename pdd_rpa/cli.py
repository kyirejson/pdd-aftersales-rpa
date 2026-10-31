from __future__ import annotations

import argparse
import getpass
import json
import secrets
import threading
import webbrowser

from .config import load_config
from .console_auth import build_config_document
from .db import Repository
from .orchestrator import Orchestrator
from .connectors.desktop_message import DesktopMessageConnector
from .yto_credentials import save_yto_credentials, save_yto_windows_credential
from .yto_service_chat import YtoServiceChatConfig


def ensure_console_access(config) -> str | None:
    """Create a random local-console credential on first initialization.

    The generated password is printed once and never committed to the repository.
    Operators should replace it through their normal local setup process.
    """
    path = config.project_root / "runtime" / "console_access.json"
    if path.exists():
        return None
    password = secrets.token_urlsafe(18)
    document = build_config_document("admin", password)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return password


def build_runtime() -> tuple[Repository, Orchestrator]:
    config = load_config()
    repository = Repository(config.database_path)
    orchestrator = Orchestrator(config, repository)
    orchestrator.initialize()
    rules = json.loads((config.project_root / "config" / "rules.json").read_text(encoding="utf-8"))
    repository.seed_rules(rules)
    created_password = ensure_console_access(config)
    if created_password:
        print("已生成控制台随机访问密码（仅本次显示，请立即保存）：" + created_password)
    return repository, orchestrator


def main() -> None:
    parser = argparse.ArgumentParser(description="拼多多售后RPA控制工具")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="初始化数据库和安全配置")
    sub.add_parser("status", help="显示当前运行状态")
    serve = sub.add_parser("serve", help="启动本地控制台")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8876, type=int)
    serve.add_argument("--open-browser", action="store_true")
    sub.add_parser(
        "configure-yto-login",
        help="将圆通账号密码用当前 Windows 用户的 DPAPI 加密保存",
    )
    args = parser.parse_args()

    repository, orchestrator = build_runtime()
    if args.command == "init":
        print("初始化完成；当前为observe模式，真实外部操作关闭。")
    elif args.command == "status":
        print(json.dumps(repository.dashboard(), ensure_ascii=False, indent=2))
    elif args.command == "serve":
        import uvicorn

        if args.open_browser:
            threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}/")).start()
        uvicorn.run("pdd_rpa.web:app", host=args.host, port=args.port, reload=False)
    elif args.command == "configure-yto-login":
        message = DesktopMessageConnector(
            repository,
            load_config().project_root / "runtime" / "evidence",
        )
        yto = YtoServiceChatConfig.from_raw(message.get_config()["platforms"]["yto"])
        username = input("圆通账号：").strip()
        password = getpass.getpass("圆通密码：")
        save_yto_credentials(yto.credential_file, username, password)
        save_yto_windows_credential(username, password)
        print("圆通自动登录凭据已保存到 Windows 凭据管理器，并保留 DPAPI 迁移副本。")


if __name__ == "__main__":
    main()
