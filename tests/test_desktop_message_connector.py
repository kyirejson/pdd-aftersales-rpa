from pathlib import Path
from uuid import uuid4

from pdd_rpa.components import validate_component
from pdd_rpa.connectors.desktop_message import DesktopMessageConnector, ROLE_TYPES
from pdd_rpa.db import Repository
from pdd_rpa.desktop_executor import DesktopExecutionError


def _component(component_id: str, component_type: str, application: str) -> dict:
    return validate_component(
        {
            "component_id": component_id,
            "component_type": component_type,
            "name": component_id,
            "application": application,
            "locator": {
                "window": {"title": application, "reference_size": {"width": 1000, "height": 700}},
                "relative_point": {"x_ratio": 0.5, "y_ratio": 0.5},
            },
            "config": {},
        }
    )


def test_message_connector_requires_captured_components_before_writes():
    database = Path("runtime") / f"test-message-{uuid4().hex}.db"
    repo = Repository(database)
    repo.initialize()
    connector = DesktopMessageConnector(repo, Path("runtime") / "evidence")
    connector.ensure_config()

    health = connector.health().data
    assert health["mode"] == "hybrid_playwright_desktop"
    assert health["write_enabled"] is False
    assert "组件不存在" in "；".join(health["platforms"]["wecom"]["reasons"])

    config = connector.get_config()
    for platform_config in config["platforms"].values():
        for role, component_id in platform_config["roles"].items():
            repo.save_component(
                _component(component_id, ROLE_TYPES[role], platform_config["application"])
            )
    config["enabled"] = True
    connector.update_config(config)

    health = connector.health().data
    assert health["write_enabled"] is True
    assert all(item["ready"] for item in health["platforms"].values())
    database.unlink(missing_ok=True)


def test_qq_components_are_installed_with_first_result_and_header_guard():
    database = Path("runtime") / f"test-message-qq-{uuid4().hex}.db"
    repo = Repository(database)
    repo.initialize()
    connector = DesktopMessageConnector(repo, Path("runtime") / "evidence")
    connector.ensure_config()

    qq = connector.get_config()["platforms"]["qq"]
    assert qq["application"] == "QQ"
    assert connector.preflight_readiness("qq")["ready"] is True
    result = repo.get_component(qq["roles"]["search_result"])
    header = repo.get_component(qq["roles"]["group_header"])
    assert result["locator"]["relative_point"]["y"] == 157
    assert result["locator"]["semantic"]["control_types"] == ["ListItem"]
    assert header["locator"]["semantic"]["class_name"] == "chat-header__contact-name"
    database.unlink(missing_ok=True)


def test_message_readback_tolerates_ocr_spacing_but_not_tracking_number_drift():
    assert DesktopMessageConnector._readback_matches(
        "【快递】示例团队&中通对接群",
        "（ 快 递 ] 示 例 团 队 & 中 通 对 接 群",
    )
    assert DesktopMessageConnector._readback_matches(
        "79129033058930+拦截",
        "79129033058930 + 拦 截",
    )
    assert not DesktopMessageConnector._readback_matches(
        "79129033058930+拦截",
        "79129033058939 + 拦 截",
    )


def test_sent_message_readback_tolerates_bounded_numeric_ocr_drift():
    assert DesktopMessageConnector._sent_message_matches(
        "465598559502964+拦截",
        "465S98559S029 +",
    )
    assert DesktopMessageConnector._sent_message_matches(
        "79132424816409+拦截",
        "7913242481 M09 + 拦 截",
    )
    assert not DesktopMessageConnector._sent_message_matches(
        "79129033058930+拦截",
        "79111111118930 + 拦 截",
    )
    assert not DesktopMessageConnector._sent_message_matches(
        "79129033058930+拦截",
        "79129033058930",
    )
    assert DesktopMessageConnector._sent_message_matches(
        "SF5119775300257+拦截",
        "5F5119775300257 + 拦 截",
    )


def test_desktop_send_confirmation_polls_until_delayed_echo(monkeypatch):
    database = Path("runtime") / f"test-message-confirm-{uuid4().hex}.db"
    repo = Repository(database)
    repo.initialize()
    connector = DesktopMessageConnector(repo, Path("runtime") / "evidence")
    reads = iter(["", "465S98559S029 +"])
    sleeps: list[float] = []

    class FakeWindow:
        def set_focus(self):
            raise AssertionError("首轮轮询已确认，不应重新激活窗口")

    class FakeExecutor:
        def _resolve_window(self, _component):
            return FakeWindow(), None, None

        def _read_captured_ocr(self, _component, _window):
            value = next(reads)
            if not value:
                from pdd_rpa.desktop_executor import DesktopExecutionError

                raise DesktopExecutionError("Windows OCR 未读到组件文本")
            return value

    monkeypatch.setattr(
        "pdd_rpa.connectors.desktop_message.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )
    observed = connector._confirm_desktop_send(
        FakeExecutor(),
        {"config": {}, "locator": {"relative_point": {"x_ratio": 0.5, "y_ratio": 0.5}}},
        "wechat",
        "465598559502964+拦截",
    )

    assert observed == "465S98559S029 +"
    assert sleeps == [0.8]
    database.unlink(missing_ok=True)


def test_desktop_send_confirmation_rotates_ocr_regions(monkeypatch):
    database = Path("runtime") / f"test-message-confirm-regions-{uuid4().hex}.db"
    repo = Repository(database)
    repo.initialize()
    connector = DesktopMessageConnector(repo, Path("runtime") / "evidence")
    regions = []
    reads = iter(["unrelated", "40000000000000 + 拦截"])

    class FakeExecutor:
        def _resolve_window(self, _component):
            return type("Window", (), {"set_focus": lambda self: None})(), None, None

        def _read_captured_ocr(self, component, _window):
            regions.append(component["config"]["ocr_dynamic_region"])
            return next(reads)

    monkeypatch.setattr("pdd_rpa.connectors.desktop_message.time.sleep", lambda _seconds: None)
    observed = connector._confirm_desktop_send(
        FakeExecutor(),
        {"config": {}, "locator": {"relative_point": {"x_ratio": 0.5, "y_ratio": 0.5}}},
        "wecom",
        "40000000000000+拦截",
    )

    assert observed == "40000000000000 + 拦截"
    assert regions == ["right_chat_recent", "right_chat_recent_wide"]
    database.unlink(missing_ok=True)


def test_desktop_send_confirmation_rejects_text_left_in_compose(monkeypatch):
    database = Path("runtime") / f"test-message-compose-{uuid4().hex}.db"
    repo = Repository(database)
    repo.initialize()
    connector = DesktopMessageConnector(repo, Path("runtime") / "evidence")
    monkeypatch.setattr(connector, "_desktop_compose_is_empty", lambda *_args: False)
    monkeypatch.setattr(connector, "_press_desktop_send_enter", lambda *_args: None)

    class FakeExecutor:
        def _resolve_window(self, _component):
            raise AssertionError("输入框未清空时不应执行聊天历史 OCR")

    try:
        connector._confirm_desktop_send(
            FakeExecutor(),
            {"config": {}, "locator": {}},
            "wecom",
            "40000000000000+拦截",
            {"component_id": "CMP-MSG-WECOM-MESSAGE-INPUT"},
        )
        raise AssertionError("未清空的消息输入框必须失败")
    except DesktopExecutionError as exc:
        assert "补按 Enter 后消息仍停留在输入框" in str(exc)
    database.unlink(missing_ok=True)


def test_desktop_send_uses_enter_only_when_click_did_not_clear_compose(monkeypatch):
    database = Path("runtime") / f"test-message-enter-fallback-{uuid4().hex}.db"
    repo = Repository(database)
    repo.initialize()
    connector = DesktopMessageConnector(repo, Path("runtime") / "evidence")
    empty_states = iter([False, True])
    enters = []
    trace = []
    monkeypatch.setattr(
        connector,
        "_desktop_compose_is_empty",
        lambda *_args: next(empty_states),
    )
    monkeypatch.setattr(
        connector,
        "_press_desktop_send_enter",
        lambda *_args: enters.append(True),
    )
    monkeypatch.setattr("pdd_rpa.connectors.desktop_message.time.sleep", lambda _seconds: None)

    class FakeExecutor:
        def _resolve_window(self, _component):
            return type("Window", (), {"set_focus": lambda self: None})(), None, None

        def _read_captured_ocr(self, _component, _window):
            return "40000000000000 + 拦截"

    observed = connector._confirm_desktop_send(
        FakeExecutor(),
        {"config": {}, "locator": {"relative_point": {"x_ratio": 0.5, "y_ratio": 0.5}}},
        "wecom",
        "40000000000000+拦截",
        {"component_id": "CMP-MSG-WECOM-MESSAGE-INPUT"},
        trace,
    )

    assert observed
    assert enters == [True]
    assert trace[-1]["role"] == "send_enter_fallback"
    database.unlink(missing_ok=True)


def test_wechat_confirmation_never_uses_compose_overlapping_wide_crop(monkeypatch):
    database = Path("runtime") / f"test-message-wechat-region-{uuid4().hex}.db"
    repo = Repository(database)
    repo.initialize()
    connector = DesktopMessageConnector(repo, Path("runtime") / "evidence")
    regions = []

    class FakeExecutor:
        def _resolve_window(self, _component):
            return type("Window", (), {"set_focus": lambda self: None})(), None, None

        def _read_captured_ocr(self, component, _window):
            regions.append(component["config"]["ocr_dynamic_region"])
            return "40000000000000 + 拦截"

    observed = connector._confirm_desktop_send(
        FakeExecutor(),
        {"config": {}, "locator": {"relative_point": {"x_ratio": 0.5, "y_ratio": 0.5}}},
        "wechat",
        "40000000000000+拦截",
    )
    assert observed
    assert regions == ["wechat_sent_bubble"]
    database.unlink(missing_ok=True)


def test_message_config_enforces_safe_initial_send_wait():
    database = Path("runtime") / f"test-message-send-wait-{uuid4().hex}.db"
    repo = Repository(database)
    repo.initialize()
    connector = DesktopMessageConnector(repo, Path("runtime") / "evidence")
    config = connector.get_config()
    config["platforms"]["wechat"]["after_send_wait_seconds"] = 0.8
    repo.set_json_setting("desktop_message_connector_config", config)

    normalized = connector.get_config()

    assert normalized["platforms"]["wechat"]["after_send_wait_seconds"] == 1.5
    database.unlink(missing_ok=True)


def test_message_config_migrates_away_obsolete_sent_echo_roles():
    database = Path("runtime") / f"test-message-migration-{uuid4().hex}.db"
    repo = Repository(database)
    repo.initialize()
    connector = DesktopMessageConnector(repo, Path("runtime") / "evidence")
    connector.ensure_config()
    legacy = connector.get_config()
    legacy["version"] = 1
    legacy["platforms"]["wechat"]["roles"]["sent_echo"] = "CMP-MSG-WECHAT-SENT-ECHO"
    repo.set_json_setting("desktop_message_connector_config", legacy)

    connector.ensure_config()
    migrated = connector.get_config()

    assert migrated["version"] >= 3
    assert "sent_echo" not in migrated["platforms"]["wechat"]["roles"]
    database.unlink(missing_ok=True)


def test_message_send_requires_exact_history_confirmation(monkeypatch):
    database = Path("runtime") / f"test-message-send-{uuid4().hex}.db"
    repo = Repository(database)
    repo.initialize()
    connector = DesktopMessageConnector(repo, Path("runtime") / "evidence")
    connector.ensure_config()
    config = connector.get_config()
    wechat = config["platforms"]["wechat"]
    for role, component_id in wechat["roles"].items():
        repo.save_component(_component(component_id, ROLE_TYPES[role], wechat["application"]))
    config["enabled"] = True
    connector.update_config(config)

    expected_group = "示例韵达沟通群"
    calls: list[str] = []

    class FakeExecutor:
        def __init__(self, _evidence_dir):
            pass

        def execute(self, component, context):
            component_id = component["component_id"]
            calls.append(component_id)
            if component_id == wechat["roles"]["group_header"]:
                context["group_header"] = expected_group
                return expected_group
            return True

    monkeypatch.setattr("pdd_rpa.connectors.desktop_message.DesktopComponentExecutor", FakeExecutor)
    monkeypatch.setattr("pdd_rpa.connectors.desktop_message.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(connector, "_save_evidence", lambda *_args, **_kwargs: "evidence.png")
    monkeypatch.setattr(
        connector,
        "_confirm_desktop_send",
        lambda *_args, **_kwargs: "40000000000000+拦截",
    )

    result = connector.send(
        {"platform": "wechat", "group_name": expected_group},
        "40000000000000+拦截",
    )

    assert result.ok is True
    assert result.data["sent"] is True
    assert result.data["read_back"] is True
    assert result.data["sent_echo"] == "40000000000000+拦截"
    assert calls == [
        wechat["roles"]["group_header"],
        wechat["roles"]["message_input"],
        wechat["roles"]["send_button"],
    ]
    database.unlink(missing_ok=True)


def test_message_preflight_can_be_ready_while_real_sending_remains_disabled():
    database = Path("runtime") / f"test-message-preflight-{uuid4().hex}.db"
    repo = Repository(database)
    repo.initialize()
    connector = DesktopMessageConnector(repo, Path("runtime") / "evidence")
    connector.ensure_config()
    config = connector.get_config()
    wecom = config["platforms"]["wecom"]
    for role in ("search_input", "search_result", "group_header"):
        repo.save_component(_component(wecom["roles"][role], ROLE_TYPES[role], wecom["application"]))
    readiness = connector.preflight_readiness("wecom")
    assert readiness["ready"] is True
    assert connector.health().data["write_enabled"] is False
    database.unlink(missing_ok=True)


def test_wecom_preflight_reuses_current_exact_group_before_search(monkeypatch):
    database = Path("runtime") / f"test-message-reuse-{uuid4().hex}.db"
    repo = Repository(database)
    repo.initialize()
    connector = DesktopMessageConnector(repo, Path("runtime") / "evidence")
    connector.ensure_config()
    wecom = connector.get_config()["platforms"]["wecom"]
    for role in ("search_input", "search_result", "group_header"):
        repo.save_component(_component(wecom["roles"][role], ROLE_TYPES[role], wecom["application"]))

    expected_group = "【快递】示例团队&中通对接群"
    calls: list[str] = []

    class FakeExecutor:
        def __init__(self, _evidence_dir):
            pass

        def execute(self, component, context):
            component_id = component["component_id"]
            calls.append(component_id)
            if component_id == wecom["roles"]["group_header"]:
                context["group_header"] = expected_group
                return expected_group
            raise AssertionError(f"不应搜索已打开的群：{component_id}")

    monkeypatch.setattr("pdd_rpa.connectors.desktop_message.DesktopComponentExecutor", FakeExecutor)
    monkeypatch.setattr(connector, "_save_evidence", lambda *_args, **_kwargs: "evidence.png")

    result = connector.preflight_group("wecom", expected_group)

    assert result.ok is True
    assert calls == [wecom["roles"]["group_header"]]
    assert result.data["trace"][0]["phase"] == "reuse_probe"
    database.unlink(missing_ok=True)


def test_wechat_blank_chat_falls_back_to_native_search_before_reuse(monkeypatch):
    database = Path("runtime") / f"test-message-wechat-search-fallback-{uuid4().hex}.db"
    repo = Repository(database)
    repo.initialize()
    connector = DesktopMessageConnector(repo, Path("runtime") / "evidence")
    connector.ensure_config()
    wechat = connector.get_config()["platforms"]["wechat"]
    for role in ("search_input", "group_header"):
        repo.save_component(
            _component(wechat["roles"][role], ROLE_TYPES[role], wechat["application"])
        )

    expected_group = "示例韵达沟通群"
    calls: list[str] = []
    header_reads = 0

    class FakeExecutor:
        def __init__(self, _evidence_dir):
            pass

        def execute(self, component, context):
            nonlocal header_reads
            component_id = component["component_id"]
            calls.append(component_id)
            if component_id == wechat["roles"]["group_header"]:
                header_reads += 1
                value = "" if header_reads == 1 else expected_group
                context["group_header"] = value
                return value
            return True

    monkeypatch.setattr(
        "pdd_rpa.connectors.desktop_message.DesktopComponentExecutor", FakeExecutor
    )
    monkeypatch.setattr(
        "pdd_rpa.connectors.desktop_message.time.sleep", lambda _seconds: None
    )
    monkeypatch.setattr(
        connector, "_save_evidence", lambda *_args, **_kwargs: "evidence.png"
    )

    result = connector.preflight_group("wechat", expected_group)

    assert result.ok is True
    assert calls == [
        wechat["roles"]["group_header"],
        wechat["roles"]["search_input"],
        wechat["roles"]["group_header"],
    ]
    assert "search_result" not in wechat["roles"]
    assert result.data["trace"][1]["role"] == "search_input"
    assert result.data["trace"][2]["role"] == "group_header"
    assert result.data["trace"][0]["phase"] == "reuse_probe"
    database.unlink(missing_ok=True)


def test_wecom_masked_group_uses_prefix_only_for_search():
    full = "示例图书公司-顺丰专属服务群-510****801"

    assert DesktopMessageConnector._search_group_name(full) == (
        "示例图书公司-顺丰专属服务群"
    )
    assert DesktopMessageConnector._search_group_name("示例供应商 D") == "示例供应商 D"


def test_message_send_buttons_upgrade_to_template_guard():
    database = Path("runtime") / f"test-message-template-guard-{uuid4().hex}.db"
    repo = Repository(database)
    repo.initialize()
    connector = DesktopMessageConnector(repo, Path("runtime") / "evidence")
    connector.ensure_config()
    config = connector.get_config()
    for platform_config in (
        config["platforms"]["wecom"],
        config["platforms"]["wechat"],
    ):
        component_id = platform_config["roles"]["send_button"]
        repo.save_component(
            _component(component_id, ROLE_TYPES["send_button"], platform_config["application"])
        )

    connector.ensure_config()

    expected_thresholds = {"wecom": 0.85, "wechat": 0.82}
    for platform, threshold in expected_thresholds.items():
        component_id = config["platforms"][platform]["roles"]["send_button"]
        component = repo.get_component(component_id)
        assert component["config"]["require_template_match"] is True
        assert component["config"]["template_min_similarity"] == threshold
        assert component["config"]["template_search_radius_px"] == 6
    database.unlink(missing_ok=True)


def test_wechat_config_migrates_obsolete_search_result_role_out():
    database = Path("runtime") / f"test-message-wechat-search-{uuid4().hex}.db"
    repo = Repository(database)
    repo.initialize()
    connector = DesktopMessageConnector(repo, Path("runtime") / "evidence")
    connector.ensure_config()
    legacy = connector.get_config()
    legacy["version"] = 4
    legacy["platforms"]["wechat"]["roles"]["search_result"] = (
        "CMP-MSG-WECHAT-SEARCH-RESULT"
    )
    legacy["platforms"]["wechat"]["after_search_wait_seconds"] = 0.6
    repo.set_json_setting("desktop_message_connector_config", legacy)

    connector.ensure_config()
    upgraded = connector.get_config()
    wechat = upgraded["platforms"]["wechat"]
    readiness = connector.preflight_readiness("wechat")

    assert upgraded["version"] >= 5
    assert "search_result" not in wechat["roles"]
    assert wechat["after_search_wait_seconds"] == 1.2
    assert "after_open_wait_seconds" not in wechat
    assert readiness["required_roles"] == ["search_input", "group_header"]
    database.unlink(missing_ok=True)


def test_wechat_search_input_upgrade_prefers_native_search_edit():
    database = Path("runtime") / f"test-message-wechat-search-input-{uuid4().hex}.db"
    repo = Repository(database)
    repo.initialize()
    connector = DesktopMessageConnector(repo, Path("runtime") / "evidence")
    connector.ensure_config()
    wechat = connector.get_config()["platforms"]["wechat"]
    component = _component(
        wechat["roles"]["search_input"],
        ROLE_TYPES["search_input"],
        wechat["application"],
    )
    repo.save_component(component)

    connector.ensure_config()
    upgraded = repo.get_component(wechat["roles"]["search_input"])
    prepared_header = connector._role_component(
        _component(
            wechat["roles"]["group_header"],
            ROLE_TYPES["group_header"],
            wechat["application"],
        ),
        "group_header",
    )

    assert upgraded["config"]["semantic_role"] == "wechat_group_search_input"
    assert upgraded["locator"]["semantic"] == {
        "names": ["搜索"],
        "control_types": ["Edit"],
    }
    assert prepared_header["config"]["ocr_dynamic_region"] == "wechat_group_header"
    database.unlink(missing_ok=True)


def test_wecom_search_result_upgrade_removes_unsafe_generic_point():
    database = Path("runtime") / f"test-message-wecom-search-{uuid4().hex}.db"
    repo = Repository(database)
    repo.initialize()
    connector = DesktopMessageConnector(repo, Path("runtime") / "evidence")
    connector.ensure_config()
    wecom = connector.get_config()["platforms"]["wecom"]
    component = _component(
        wecom["roles"]["search_result"],
        ROLE_TYPES["search_result"],
        wecom["application"],
    )
    component["locator"]["point_window"] = {
        "title": "SearchResultWindow2",
        "class_name": "SearchResultWindow2",
    }
    repo.save_component(component)

    connector.ensure_config()
    upgraded = repo.get_component(wecom["roles"]["search_result"])

    assert upgraded["config"]["semantic_role"] == "wecom_exact_group_search_result"
    assert upgraded["config"]["value_variable"] == "group_name"
    assert upgraded["config"]["ocr_scale"] == 3
    assert upgraded["config"]["search_retry_times"] == 2
    assert upgraded["config"]["search_retry_wait_seconds"] == 0.35
    assert "relative_point" not in upgraded["locator"]
    database.unlink(missing_ok=True)


def test_yto_config_migrates_from_desktop_roles_to_profile_20_playwright():
    database = Path("runtime") / f"test-message-yto-{uuid4().hex}.db"
    repo = Repository(database)
    repo.initialize()
    connector = DesktopMessageConnector(repo, Path("runtime") / "evidence")
    legacy = connector.get_config()
    legacy["version"] = 2
    legacy["platforms"]["yto"] = {
        "enabled": True,
        "application": "圆通客户管家",
        "roles": {
            "login_button": "CMP-MSG-YTO-LOGIN",
            "service_entry": "CMP-MSG-YTO-SERVICE-ENTRY",
            "message_input": "CMP-MSG-YTO-MESSAGE-INPUT",
            "send_button": "CMP-MSG-YTO-SEND",
        },
    }
    repo.set_json_setting("desktop_message_connector_config", legacy)
    connector.ensure_config()

    yto = connector.get_config()["platforms"]["yto"]
    assert yto["transport"] == "playwright"
    assert yto["source_profile_directory"] == "Profile 20"
    assert yto["roles"] == {}
    assert connector.preflight_readiness("yto")["required_roles"] == []
    database.unlink(missing_ok=True)


def test_yto_preflight_and_send_use_playwright_writer(monkeypatch):
    database = Path("runtime") / f"test-message-yto-writer-{uuid4().hex}.db"
    repo = Repository(database)
    repo.initialize()
    connector = DesktopMessageConnector(repo, Path("runtime") / "evidence")
    connector.ensure_config()
    config = connector.get_config()
    config["enabled"] = True
    connector.update_config(config)
    expected_group = "示例客户服务群"
    calls: list[tuple[str, str, str]] = []

    class FakeWriter:
        def __init__(self, writer_config, _evidence_dir):
            assert writer_config.source_profile_directory == "Profile 20"

        def preflight(self, group):
            calls.append(("preflight", group, ""))
            return {"sent": False, "group_name": group, "evidence": "preflight.png"}

        def credential_readiness(self):
            return {
                "ready": True,
                "source": "test_fixture",
                "windows_user": "test-user",
            }

        def send(self, group, message):
            calls.append(("send", group, message))
            return {"sent": True, "group_name": group, "evidence": "send.png"}

    monkeypatch.setattr("pdd_rpa.connectors.desktop_message.YtoServiceChatWriter", FakeWriter)
    preflight = connector.preflight_group("yto", expected_group)
    sent = connector.send(
        {"platform": "yto", "group_name": expected_group},
        "YT123456789+拦截",
    )

    assert preflight.ok is True
    assert sent.ok is True
    assert calls == [
        ("preflight", expected_group, ""),
        ("send", expected_group, "YT123456789+拦截"),
    ]
    database.unlink(missing_ok=True)
