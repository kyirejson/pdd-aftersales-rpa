from __future__ import annotations

"""Desktop and Playwright messenger connector for interception requests.

Enterprise WeChat and WeChat use captured desktop components.  The YTO web
service uses one isolated, persistent Playwright-controlled Edge profile.
"""

from copy import deepcopy
from datetime import datetime
from pathlib import Path
import re
import time
from typing import Any

from .base import ConnectorResult, MessageConnector
from ..components import validate_component
from ..db import Repository
from ..desktop_executor import DesktopComponentExecutor, DesktopExecutionError
from ..yto_service_chat import (
    YtoServiceChatConfig,
    YtoServiceChatError,
    YtoServiceChatWriter,
)


SETTING_KEY = "desktop_message_connector_config"


ROLE_TYPES = {
    "search_input": "InputDesktopText",
    "search_result": "ClickDesktopElement",
    "group_header": "ReadDesktopText",
    "message_input": "InputDesktopText",
    "send_button": "ClickDesktopElement",
}


DEFAULT_CONFIG: dict[str, Any] = {
    "version": 5,
    # This switch stays false until the operator captures every platform's
    # controls and explicitly enables the connector from the routing page.
    "enabled": False,
    "platforms": {
        "wecom": {
            "enabled": True,
            "application": "企业微信",
            "roles": {
                "search_input": "CMP-MSG-WECOM-SEARCH-INPUT",
                "search_result": "CMP-MSG-WECOM-SEARCH-RESULT",
                "group_header": "CMP-MSG-WECOM-GROUP-HEADER",
                "message_input": "CMP-MSG-WECOM-MESSAGE-INPUT",
                "send_button": "CMP-MSG-WECOM-SEND",
            },
            "after_search_wait_seconds": 0.6,
            "after_open_wait_seconds": 0.6,
            "after_send_wait_seconds": 1.5,
        },
        "wechat": {
            "enabled": True,
            "application": "微信",
            "roles": {
                "search_input": "CMP-MSG-WECHAT-SEARCH-INPUT",
                "group_header": "CMP-MSG-WECHAT-GROUP-HEADER",
                "message_input": "CMP-MSG-WECHAT-MESSAGE-INPUT",
                "send_button": "CMP-MSG-WECHAT-SEND",
            },
            "after_search_wait_seconds": 1.2,
            "after_send_wait_seconds": 1.5,
        },
        "qq": {
            "enabled": True,
            "application": "QQ",
            "roles": {
                "search_input": "CMP-MSG-QQ-SEARCH-INPUT",
                "search_result": "CMP-MSG-QQ-SEARCH-RESULT",
                "group_header": "CMP-MSG-QQ-GROUP-HEADER",
                "message_input": "CMP-MSG-QQ-MESSAGE-INPUT",
                "send_button": "CMP-MSG-QQ-SEND",
            },
            "after_search_wait_seconds": 0.8,
            "after_open_wait_seconds": 0.8,
            "after_send_wait_seconds": 1.5,
        },
        "yto": {
            "enabled": True,
            "application": "圆通客户管家",
            "transport": "playwright",
            "url": "https://kh.yto.net.cn/new/home",
            "login_url": "https://kh.yto.net.cn/new/login",
            "edge_profile": "Profile 20",
            "playwright_user_data_dir": "runtime/edge_profiles/yto_playwright",
            "source_edge_user_data_dir": str(
                Path.home() / "AppData/Local/Microsoft/Edge/User Data"
            ),
            "source_profile_directory": "Profile 20",
            "cdp_endpoint": "http://127.0.0.1:9223",
            "expected_group_name": "示例客户服务群",
            "connect_timeout_seconds": 15,
            "action_timeout_seconds": 12,
            "credential_file": str(
                Path.home()
                / "AppData/Local/PDD_RPA/secrets/yto_credentials.dpapi"
            ),
            "roles": {},
        },
    },
}


class DesktopMessageConfigError(ValueError):
    pass


def _required_roles(platform: str) -> tuple[str, ...]:
    if platform == "yto":
        return ()
    return (*_group_open_roles(platform), "message_input", "send_button")


def _group_open_roles(platform: str) -> tuple[str, ...]:
    if platform == "yto":
        return ()
    if platform == "wechat":
        # Current WeChat opens the right chat pane as soon as its native search
        # receives the group name. Clicking the left result closes that pane.
        return ("search_input", "group_header")
    return ("search_input", "search_result", "group_header")


def validate_config(definition: dict[str, Any] | None) -> dict[str, Any]:
    """Merge persisted settings with the fixed supported-platform schema."""
    incoming = dict(definition or {})
    result = deepcopy(DEFAULT_CONFIG)
    result["enabled"] = bool(incoming.get("enabled", result["enabled"]))
    try:
        result["version"] = max(5, int(incoming.get("version", result["version"])))
    except (TypeError, ValueError) as exc:
        raise DesktopMessageConfigError("消息发送组件版本必须为正整数") from exc

    raw_platforms = incoming.get("platforms") or {}
    if not isinstance(raw_platforms, dict):
        raise DesktopMessageConfigError("消息发送平台配置必须是对象")
    unknown = set(raw_platforms) - set(DEFAULT_CONFIG["platforms"])
    if unknown:
        raise DesktopMessageConfigError(f"不支持的消息平台：{sorted(unknown)}")
    for platform, defaults in DEFAULT_CONFIG["platforms"].items():
        update = raw_platforms.get(platform) or {}
        if not isinstance(update, dict):
            raise DesktopMessageConfigError(f"{platform} 平台配置必须是对象")
        merged = deepcopy(defaults)
        if platform == "yto":
            # Do not retain version-2 UIA wait/locator fields in the new web
            # transport.  Only explicit Playwright settings survive migration.
            merged.update(
                {
                    key: value
                    for key, value in update.items()
                    if key in defaults and key != "roles"
                }
            )
        else:
            merged.update({key: value for key, value in update.items() if key != "roles"})
        merged["enabled"] = bool(merged.get("enabled", True))
        merged["application"] = str(merged.get("application") or "").strip()
        if not merged["application"]:
            raise DesktopMessageConfigError(f"{platform} 必须填写应用窗口标识")
        roles = dict(defaults["roles"])
        raw_roles = update.get("roles") or {}
        if not isinstance(raw_roles, dict):
            raise DesktopMessageConfigError(f"{platform} roles 必须是对象")
        # Sent confirmation is now read dynamically from the newest outgoing
        # bubble after dispatch. It is not a reusable fixed UI component, so
        # migrate the obsolete sent_echo capture slot out of persisted roles.
        raw_roles = {
            key: value
            for key, value in raw_roles.items()
            if key != "sent_echo"
            and not (platform == "wechat" and key == "search_result")
        }
        # Version 2 stored YTO UIA captures.  Playwright owns the whole YTO
        # page now, so discard those obsolete roles during migration.
        if platform == "yto":
            raw_roles = {}
        unknown_roles = set(raw_roles) - set(roles)
        if unknown_roles:
            raise DesktopMessageConfigError(f"{platform} 存在未知组件角色：{sorted(unknown_roles)}")
        for role, default_component_id in list(roles.items()):
            # Earlier preview-only settings persisted empty placeholders.
            # Treat an empty value as "use the fixed capture slot" so a
            # backend upgrade immediately recognizes future designer captures.
            value = str(raw_roles.get(role) or default_component_id or "").strip()
            roles[role] = value
        merged["roles"] = roles
        if platform == "wechat":
            # WeChat no longer opens a left-side result, so this historical
            # post-result delay must not survive configuration migration.
            merged.pop("after_open_wait_seconds", None)
        for key in ("after_search_wait_seconds", "after_open_wait_seconds", "after_send_wait_seconds"):
            if key not in merged:
                continue
            try:
                merged[key] = max(0.1, min(float(merged.get(key, 0.6)), 5.0))
            except (TypeError, ValueError) as exc:
                raise DesktopMessageConfigError(f"{platform} 的 {key} 必须是 0.1 到 5 秒") from exc
        if platform in {"wecom", "wechat", "qq"}:
            # Desktop chat bubbles can be painted after the send-button
            # action has returned. Persisted 0.8-second settings were too
            # short and produced false uncertain-write results.
            merged["after_send_wait_seconds"] = max(
                1.5, float(merged["after_send_wait_seconds"])
            )
        if platform == "wechat":
            # Give the automatically opened right pane enough time to paint
            # before the exact group-header OCR. A tight historical 0.6 second
            # wait caused false mismatches on a busy desktop.
            merged["after_search_wait_seconds"] = max(
                1.2, float(merged["after_search_wait_seconds"])
            )
        if platform == "yto":
            yto_reasons = YtoServiceChatConfig.from_raw(merged).validate()
            if yto_reasons:
                raise DesktopMessageConfigError("；".join(yto_reasons))
        result["platforms"][platform] = merged
    return result


class DesktopMessageConnector(MessageConnector):
    """Send one message only through fully captured/verified desktop controls."""

    name = "message.desktop_components"

    def __init__(self, repository: Repository, evidence_dir: Path):
        self.repository = repository
        self.evidence_dir = Path(evidence_dir)

    def get_config(self) -> dict[str, Any]:
        return validate_config(self.repository.get_json_setting(SETTING_KEY, DEFAULT_CONFIG))

    def update_config(self, definition: dict[str, Any]) -> dict[str, Any]:
        config = validate_config(definition)
        current = self.get_config()
        config["version"] = int(current.get("version", 1)) + 1
        self.repository.set_json_setting(SETTING_KEY, config)
        return config

    def ensure_config(self) -> None:
        if not self.repository.get_json_setting(SETTING_KEY):
            self.repository.set_json_setting(SETTING_KEY, DEFAULT_CONFIG)
        else:
            # Persist migrations so UI and health show the complete schema.
            self.repository.set_json_setting(SETTING_KEY, self.get_config())
        self._upgrade_wechat_search_input_component()
        self._upgrade_wecom_exact_group_search_component()
        self._ensure_qq_components()
        self._upgrade_message_send_button_guards()

    def _ensure_qq_components(self) -> None:
        """Install the inspected QQNT controls for the configured desktop client.

        QQNT exposes the opened group header and Send button through UIA.  Its
        search/editor surfaces are less stable, so those two inputs retain a
        proportional point guarded by the captured 960x640 window geometry.
        The search-result point is intentionally the first QQ group result, as
        requested by the operator; the exact opened header is still verified
        before the message editor can be populated.
        """

        window = {
            "title": "QQ",
            "class_name": "Chrome_WidgetWin_1",
            "reference_size": {"width": 960, "height": 640},
        }

        def definition(
            component_id: str,
            name: str,
            component_type: str,
            *,
            x: int,
            y: int,
            class_name: str = "",
            semantic: dict[str, Any] | None = None,
            config: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            locator: dict[str, Any] = {
                "window": deepcopy(window),
                "point_window": {"title": "QQ", "class_name": "Chrome_WidgetWin_1"},
                "relative_point": {
                    "x": x,
                    "y": y,
                    "x_ratio": x / 960,
                    "y_ratio": y / 640,
                },
                "resolution_fallback": True,
            }
            if class_name:
                locator["class_name"] = class_name
            else:
                locator["name"] = name
            if semantic:
                locator["semantic"] = semantic
            return validate_component(
                {
                    "component_id": component_id,
                    "component_type": component_type,
                    "name": name,
                    "application": "QQ",
                    "locator": locator,
                    "config": dict(config or {}),
                    "before_assertions": ["QQ窗口唯一", "窗口尺寸在容差内"],
                    "on_failure": "MANUAL_REVIEW",
                    "version": 1,
                }
            )

        definitions = (
            definition(
                "CMP-MSG-QQ-SEARCH-INPUT",
                "QQ群搜索框",
                "InputDesktopText",
                x=164,
                y=66,
            ),
            definition(
                "CMP-MSG-QQ-SEARCH-RESULT",
                "QQ第一条群搜索结果",
                "ClickDesktopElement",
                x=216,
                y=157,
                class_name="list-item search-result-item empty-summary selected",
                semantic={
                    "control_types": ["ListItem"],
                    "class_name": "list-item search-result-item empty-summary selected",
                },
                config={"after_wait_seconds": 0.5},
            ),
            definition(
                "CMP-MSG-QQ-GROUP-HEADER",
                "QQ当前群标题",
                "ReadDesktopText",
                x=436,
                y=68,
                class_name="chat-header__contact-name",
                semantic={
                    "control_types": ["Button"],
                    "class_name": "chat-header__contact-name",
                },
                config={
                    "read_strategy": "ocr",
                    "output_variable": "group_header",
                    "ocr_width_px": 400,
                    "ocr_height_px": 80,
                    "ocr_scale": 3,
                },
            ),
            definition(
                "CMP-MSG-QQ-MESSAGE-INPUT",
                "QQ消息输入框",
                "InputDesktopText",
                x=541,
                y=525,
                class_name="ProseMirror is-empty ExEditor-qq-msg-editor ProseMirror-focused",
            ),
            definition(
                "CMP-MSG-QQ-SEND",
                "QQ发送按钮",
                "ClickDesktopElement",
                x=692,
                y=608,
                class_name="send-msg",
                semantic={"control_types": ["Button"], "class_name": "send-msg"},
                config={"after_wait_seconds": 0.2},
            ),
        )
        for component in definitions:
            if not self.repository.get_component(component["component_id"]):
                self.repository.save_component(component)

    def sync_yto_settings(self, definition: dict[str, Any] | None) -> dict[str, Any]:
        """Persist the file-backed YTO profile settings for isolated workers."""

        if not definition:
            return self.get_config()
        current = self.get_config()
        candidate = deepcopy(current)
        yto = dict(candidate["platforms"]["yto"])
        yto.update(dict(definition))
        yto["roles"] = {}
        candidate["platforms"]["yto"] = yto
        normalized = validate_config(candidate)
        if normalized != current:
            normalized["version"] = int(current.get("version", 3)) + 1
            self.repository.set_json_setting(SETTING_KEY, normalized)
        return normalized

    def _upgrade_message_send_button_guards(self) -> None:
        """Make each coordinate-only send button prove its local appearance.

        Chat clients draw their compose controls in custom surfaces, so their
        existing captures can legitimately have no smaller UIA child.  Keep
        that supported path, but require the saved button template to match
        the current local screen before a coordinate click is allowed.
        """

        config = self.get_config()
        thresholds = {
            "wecom": 0.85,
            "wechat": 0.82,
        }
        for platform, minimum in thresholds.items():
            platform_config = dict(config.get("platforms", {}).get(platform) or {})
            component_id = str(dict(platform_config.get("roles") or {}).get("send_button") or "").strip()
            if not component_id:
                continue
            component = self.repository.get_component(component_id)
            if not component or component.get("component_type") != ROLE_TYPES["send_button"]:
                continue
            component_config = dict(component.get("config") or {})
            desired = {
                "require_template_match": True,
                "template_min_similarity": minimum,
                "template_search_radius_px": 6,
            }
            if all(component_config.get(key) == value for key, value in desired.items()):
                continue
            component_config.update(desired)
            component["config"] = component_config
            component["version"] = max(int(component.get("version", 1) or 1), 2)
            self.repository.save_component(validate_component(component))

    def _upgrade_wechat_search_input_component(self) -> None:
        """Prefer WeChat's native Search edit over the legacy fixed point.

        Current WeChat exposes one visible ``Edit`` named ``搜索``.  Keeping the
        captured point as a guarded fallback preserves compatibility with older
        builds, while the semantic locator makes the normal path actively focus
        the real search box before clearing and entering the group name.
        """

        config = self.get_config()
        component_id = str(
            config.get("platforms", {})
            .get("wechat", {})
            .get("roles", {})
            .get("search_input")
            or ""
        ).strip()
        if not component_id:
            return
        component = self.repository.get_component(component_id)
        if not component or component.get("component_type") != ROLE_TYPES["search_input"]:
            return
        component_config = dict(component.get("config") or {})
        locator = dict(component.get("locator") or {})
        semantic = dict(locator.get("semantic") or {})
        desired_semantic = {
            "names": ["搜索"],
            "control_types": ["Edit"],
        }
        changed = False
        if component_config.get("semantic_role") != "wechat_group_search_input":
            component_config["semantic_role"] = "wechat_group_search_input"
            changed = True
        if semantic != desired_semantic:
            locator["semantic"] = desired_semantic
            changed = True
        if changed:
            component["config"] = component_config
            component["locator"] = locator
            component["version"] = max(int(component.get("version", 1) or 1), 2)
            self.repository.save_component(validate_component(component))

    def _upgrade_wecom_exact_group_search_component(self) -> None:
        """Replace Enterprise WeChat's generic result-point click as well.

        Its popup has an ``进入全局搜索`` row directly below the group result.
        Clicking a captured point after a redraw could open global search (or
        another result) instead of the requested carrier group.  The semantic
        component OCRs the popup and accepts exactly one matching group row.
        """

        config = self.get_config()
        component_id = str(
            config.get("platforms", {}).get("wecom", {}).get("roles", {}).get("search_result") or ""
        ).strip()
        if not component_id:
            return
        component = self.repository.get_component(component_id)
        if not component or component.get("component_type") != ROLE_TYPES["search_result"]:
            return
        component_config = dict(component.get("config") or {})
        locator = dict(component.get("locator") or {})
        changed = False
        desired_config = {
            "semantic_role": "wecom_exact_group_search_result",
            "value_variable": "group_name",
            "ocr_scale": 3,
            "ocr_timeout_seconds": 12,
            "chat_result_max_y_ratio": 0.72,
            "search_retry_times": 2,
            "search_retry_wait_seconds": 0.35,
            "after_wait_seconds": 0.5,
        }
        for key, value in desired_config.items():
            if component_config.get(key) != value:
                component_config[key] = value
                changed = True
        popup = dict(locator.get("point_window") or {})
        if not str(locator.get("name") or "").strip() and str(popup.get("title") or "").strip():
            locator["name"] = str(popup["title"]).strip()
            changed = True
        if not str(locator.get("class_name") or "").strip() and str(popup.get("class_name") or "").strip():
            locator["class_name"] = str(popup["class_name"]).strip()
            changed = True
        if "relative_point" in locator:
            locator.pop("relative_point", None)
            changed = True
        if changed:
            component["config"] = component_config
            component["locator"] = locator
            component["version"] = max(int(component.get("version", 1) or 1), 2)
            self.repository.save_component(validate_component(component))

    @staticmethod
    def _normalize(value: Any) -> str:
        return re.sub(r"\s+", "", str(value or ""))

    @classmethod
    def _readback_matches(cls, expected: str, observed: str) -> bool:
        """Compare native/OCR readback without weakening tracking-number checks.

        Windows OCR often inserts a space between Chinese glyphs and may read
        a decorative bracket as a parenthesis.  Group names are therefore
        compared using each meaningful Chinese/alphanumeric segment.  A sent
        message still requires the entire tracking number and the word
        ``拦截`` to be present; it never accepts a fuzzy numeric match.
        """

        expected_text = cls._normalize(expected)
        observed_text = cls._normalize(observed)
        if not expected_text or not observed_text:
            return False
        if expected_text in observed_text:
            return True
        tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]{3,}", expected_text)
        return bool(tokens) and all(token in observed_text for token in tokens)

    @staticmethod
    def _edit_distance(left: str, right: str) -> int:
        """Return Levenshtein distance for short OCR tokens."""

        if left == right:
            return 0
        if not left:
            return len(right)
        if not right:
            return len(left)
        previous = list(range(len(right) + 1))
        for left_index, left_char in enumerate(left, start=1):
            current = [left_index]
            for right_index, right_char in enumerate(right, start=1):
                current.append(
                    min(
                        current[-1] + 1,
                        previous[right_index] + 1,
                        previous[right_index - 1] + (left_char != right_char),
                    )
                )
            previous = current
        return previous[-1]

    @classmethod
    def _sent_message_matches(cls, expected: str, observed: str) -> bool:
        """Verify an interception bubble despite controlled numeric OCR drift.

        Exact text remains preferred. The fallback is restricted to numeric
        waybills, requires the interception marker (or its visible separator),
        translates common OCR glyph confusions, and accepts at most three
        edits. It is deliberately not used for group headers or free text.
        """

        if cls._readback_matches(expected, observed):
            return True
        expected_text = cls._normalize(expected)
        observed_text = cls._normalize(observed).upper()
        tracking_match = re.fullmatch(r"([A-Z]{0,5}[0-9]{8,})\+拦截", expected_text.upper())
        if not tracking_match or not observed_text:
            return False
        if "拦截" not in observed_text and "+" not in observed_text:
            return False

        confusion_map = str.maketrans(
            {
                "S": "5",
                "O": "0",
                "Q": "0",
                "D": "0",
                "I": "1",
                "L": "1",
                "Z": "2",
                "B": "8",
                "G": "6",
            }
        )
        expected_tracking = tracking_match.group(1).translate(confusion_map)
        maximum_distance = min(3, max(1, len(expected_tracking) // 5))
        minimum_length = len(expected_tracking) - maximum_distance
        maximum_length = len(expected_tracking) + maximum_distance
        for token in re.findall(r"[A-Z0-9]+", observed_text):
            normalized_token = token.translate(confusion_map)
            if len(normalized_token) < minimum_length:
                continue
            for size in range(
                minimum_length,
                min(maximum_length, len(normalized_token)) + 1,
            ):
                for start in range(0, len(normalized_token) - size + 1):
                    candidate = normalized_token[start : start + size]
                    if cls._edit_distance(expected_tracking, candidate) <= maximum_distance:
                        return True
        return False

    def _component_for_role(
        self, config: dict[str, Any], platform: str, role: str
    ) -> tuple[dict[str, Any] | None, str]:
        platform_config = config["platforms"][platform]
        component_id = str(platform_config["roles"].get(role) or "")
        if not component_id:
            return None, f"{platform} 未捕获组件：{role}"
        component = self.repository.get_component(component_id)
        if not component:
            return None, f"{platform} 组件不存在：{role} ({component_id})"
        if not component.get("enabled", True):
            return None, f"{platform} 组件已禁用：{role} ({component_id})"
        expected_type = ROLE_TYPES[role]
        if component.get("component_type") != expected_type:
            return None, f"{platform} 组件类型不匹配：{role} 应为 {expected_type}"
        captured_application = str(component.get("application") or "")
        application = str(platform_config.get("application") or "")
        if application and application not in captured_application:
            return None, f"{platform} 组件窗口不匹配：{role} 不是从“{application}”捕获"
        return component, ""

    def platform_readiness(self, platform: str) -> dict[str, Any]:
        normalized = str(platform or "").strip().lower()
        config = self.get_config()
        if normalized not in config["platforms"]:
            return {"ready": False, "reasons": [f"不支持的消息平台：{platform}"], "platform": normalized}
        platform_config = config["platforms"][normalized]
        reasons: list[str] = []
        if not config.get("enabled"):
            reasons.append("消息发送总开关未开启")
        if not platform_config.get("enabled"):
            reasons.append(f"{normalized} 平台已禁用")
        if normalized == "yto":
            yto_config = YtoServiceChatConfig.from_raw(platform_config)
            reasons.extend(yto_config.validate())
            if not reasons:
                credential = YtoServiceChatWriter(
                    yto_config, self.evidence_dir
                ).credential_readiness()
                if not credential.get("ready"):
                    reasons.append(str(credential.get("error") or "圆通自动登录凭据不可用"))
        for role in _required_roles(normalized):
            _, error = self._component_for_role(config, normalized, role)
            if error:
                reasons.append(error)
        result = {
            "ready": not reasons,
            "platform": normalized,
            "application": platform_config["application"],
            "required_roles": list(_required_roles(normalized)),
            "reasons": reasons,
        }
        if normalized == "yto":
            result["credential"] = YtoServiceChatWriter(
                YtoServiceChatConfig.from_raw(platform_config), self.evidence_dir
            ).credential_readiness()
        return result

    def health(self) -> ConnectorResult:
        config = self.get_config()
        platforms = {name: self.platform_readiness(name) for name in config["platforms"]}
        write_enabled = bool(config.get("enabled")) and all(
            item["ready"] for item in platforms.values() if config["platforms"][item["platform"]].get("enabled")
        )
        # A partially captured connector is healthy as a configured RPA
        # feature, but explicitly reports that external writes are blocked.
        return ConnectorResult(
            True,
            {
                "mode": "hybrid_playwright_desktop",
                "write_enabled": write_enabled,
                "enabled": bool(config.get("enabled")),
                "platforms": platforms,
            },
        )

    @property
    def supports_live_writes(self) -> bool:
        return bool(self.health().data.get("write_enabled"))

    def preview_send(self, group_name: str, message: str) -> ConnectorResult:
        return ConnectorResult(
            True,
            {"group_name": group_name, "message": message, "sent": False},
            evidence=["仅生成拦截消息预览，未触发桌面客户端"],
        )

    @staticmethod
    def _role_component(component: dict[str, Any], role: str) -> dict[str, Any]:
        prepared = deepcopy(component)
        config = dict(prepared.get("config") or {})
        if role in {"search_input", "search_result"}:
            config["value_variable"] = "search_group_name"
        elif role == "message_input":
            config["value_variable"] = "message"
        elif role == "group_header":
            config["output_variable"] = role
            # Native chat headers are frequently custom-rendered. Capture the
            # title region and read it with local Windows OCR rather than
            # relying on a clipboard selection that the client may not support.
            config["read_strategy"] = "ocr"
            config.setdefault("ocr_scale", 2)
            config.setdefault("ocr_width_px", 720)
            config.setdefault("ocr_height_px", 130)
            if str(prepared.get("component_id") or "") == "CMP-MSG-WECHAT-GROUP-HEADER":
                # The legacy 720px crop crossed into WeChat's left session
                # list.  A pinned target group there could falsely prove that
                # the blank right chat pane was already inside that group,
                # skipping search altogether.  Restrict reuse verification to
                # the actual right-pane title bar.
                config["ocr_dynamic_region"] = "wechat_group_header"
                config["ocr_scale"] = max(3, int(config.get("ocr_scale", 2) or 2))
        prepared["config"] = config
        return validate_component(prepared)

    @staticmethod
    def _search_group_name(group_name: str) -> str:
        """Use a searchable prefix when WeCom masks account digits with ``*``.

        A literal query such as ``...-510****801`` returns no result in the
        current WeCom client even though that masked text is the real visible
        group title.  Search only the stable title prefix, but retain the full
        configured name for result OCR and the mandatory opened-header
        readback.  This changes discovery only; it does not weaken the target
        whitelist used before message input or sending.
        """
        value = str(group_name or "").strip()
        if "*" not in value:
            return value
        prefix = re.sub(
            r"[\s\-－—_.．·]*\d*\*+\d*\s*$",
            "",
            value,
        ).strip()
        return prefix or value

    def _save_evidence(
        self,
        executor: DesktopComponentExecutor,
        component: dict[str, Any],
        platform: str,
        *,
        prefix: str = "intercept",
    ) -> str:
        """Save a screenshot from the same captured client window."""
        window, _, _ = executor._resolve_window(component)  # component identity already verified
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = self.evidence_dir / f"{prefix}_{platform}_{stamp}.png"
        window.capture_as_image().save(path)
        return str(path)

    def _confirm_desktop_send(
        self,
        executor: DesktopComponentExecutor,
        component: dict[str, Any],
        platform: str,
        expected_message: str,
        input_component: dict[str, Any] | None = None,
        trace: list[dict[str, Any]] | None = None,
    ) -> str:
        """Poll for an outgoing bubble without ever clicking Send twice.

        When the captured input component is available, an uncleared editor
        fails immediately.  OCR then runs only above the compose divider (or
        inside a detected green WeChat bubble), so text still sitting in the
        editor cannot masquerade as delivery evidence.
        """

        if input_component is not None and not self._desktop_compose_is_empty(
            executor, input_component
        ):
            # A coordinate click can be swallowed by a custom-painted client.
            # Enter is a one-shot fallback only while the original text is
            # provably still in the editor; never press it after the button
            # has already cleared the editor, which could duplicate a send.
            self._press_desktop_send_enter(executor, input_component)
            if trace is not None:
                trace.append(
                    {
                        "role": "send_enter_fallback",
                        "component_id": input_component.get("component_id", ""),
                        "value": True,
                    }
                )
            time.sleep(0.8)
            if not self._desktop_compose_is_empty(executor, input_component):
                raise DesktopExecutionError(
                    "发送按钮点击无效，补按 Enter 后消息仍停留在输入框，判定为未发送"
                )

        window, _, _ = executor._resolve_window(component)
        def make_probe(dynamic_region: str, scale: int, preprocess: str = "") -> dict[str, Any]:
            probe = deepcopy(component)
            probe_config = dict(probe.get("config") or {})
            probe_config.update(
                {
                    "read_strategy": "ocr",
                    "ocr_dynamic_region": dynamic_region,
                    "ocr_scale": scale,
                    "ocr_timeout_seconds": 12,
                    "ocr_preprocess": preprocess,
                }
            )
            probe["config"] = probe_config
            return probe

        if platform == "wechat":
            probes = [
                make_probe("wechat_sent_bubble", 4, "threshold"),
                make_probe("wechat_sent_bubble", 3),
                make_probe("wechat_sent_bubble", 4),
            ]
        elif platform == "qq":
            probes = [
                make_probe("qq_chat_recent", 3),
                make_probe("qq_chat_recent", 4, "threshold"),
                make_probe("qq_chat_recent", 2),
            ]
        else:
            probes = [
                make_probe("right_chat_recent", 2),
                make_probe("right_chat_recent_wide", 3),
                make_probe("right_chat_recent_wide", 2, "threshold"),
            ]
        last_observed = ""
        last_error = ""
        probe_index = 0

        def poll(seconds: float) -> str:
            nonlocal last_observed, last_error, probe_index
            deadline = time.monotonic() + seconds
            first = True
            while first or time.monotonic() < deadline:
                if not first:
                    time.sleep(min(0.8, max(0.0, deadline - time.monotonic())))
                first = False
                try:
                    probe = probes[probe_index % len(probes)]
                    probe_index += 1
                    observed = executor._read_captured_ocr(probe, window)
                except DesktopExecutionError as exc:
                    last_error = str(exc)
                    continue
                if observed:
                    last_observed = observed
                if self._sent_message_matches(expected_message, observed):
                    return observed
            return ""

        observed_message = poll(18.0)
        if observed_message:
            return observed_message

        # A delayed desktop repaint can leave the same chat visually stale.
        # One focus-only recheck is safe: it does not search, click a result,
        # populate the input, or resend the message.
        try:
            window.set_focus()
        except Exception:
            pass
        observed_message = poll(7.0)
        if observed_message:
            return observed_message
        raise DesktopExecutionError(
            f"发送按钮已点击，但未在当前群聊读到本次拦截消息："
            f"{last_observed or last_error or '<空>'}"
        )

    @staticmethod
    def _desktop_compose_is_empty(
        executor: DesktopComponentExecutor,
        component: dict[str, Any],
    ) -> bool:
        """Read the captured desktop editor via clipboard without writing it."""

        _window, point = executor._resolve(component)
        target = getattr(executor, "_last_resolved_target", None)
        sentinel = f"__MSG_EMPTY_{time.time_ns()}__"
        with executor._preserve_clipboard():
            executor._clipboard_set(sentinel)
            if target is not None:
                try:
                    target.click_input()
                except Exception as exc:
                    raise DesktopExecutionError("消息输入框无法聚焦回读") from exc
            elif point is not None:
                executor.mouse.click(coords=point)
            else:
                raise DesktopExecutionError("消息输入框缺少可回读定位")
            executor.keyboard.send_keys("^a")
            executor.keyboard.send_keys("^c")
            time.sleep(0.15)
            copied = str(executor._clipboard_get() or "").strip()
        # Copy on an empty editor leaves the sentinel unchanged on both
        # current desktop clients.  Some builds explicitly clear it instead.
        return copied in {"", sentinel}

    @staticmethod
    def _press_desktop_send_enter(
        executor: DesktopComponentExecutor,
        component: dict[str, Any],
    ) -> None:
        """Focus the proven composer and issue one conditional Enter fallback."""

        _window, point = executor._resolve(component)
        target = getattr(executor, "_last_resolved_target", None)
        if target is not None:
            try:
                target.click_input()
            except Exception as exc:
                raise DesktopExecutionError("消息输入框无法聚焦补按 Enter") from exc
        elif point is not None:
            executor.mouse.click(coords=point)
        else:
            raise DesktopExecutionError("消息输入框缺少 Enter 补发定位")
        executor.keyboard.send_keys("{ENTER}")

    def _open_or_reuse_desktop_chat_group(
        self,
        *,
        executor: DesktopComponentExecutor,
        components: dict[str, dict[str, Any]],
        context: dict[str, Any],
        expected_group: str,
        platform: str,
        platform_config: dict[str, Any],
        trace: list[dict[str, Any]],
    ) -> None:
        """Reuse an already open exact chat before invoking client search.

        Reading the captured header first removes an avoidable search when the
        exact group is already open. WeChat's native search opens the right
        chat pane automatically, so it verifies that header without clicking a
        left result. Enterprise WeChat and QQ retain their explicit result
        selection before the same mandatory header verification.
        """

        header = components["group_header"]
        context.pop("group_header", None)
        try:
            value = executor.execute(header, context)
            observed = str(context.get("group_header") or value or "")
            trace.append(
                {
                    "role": "group_header",
                    "component_id": header["component_id"],
                    "value": observed,
                    "phase": "reuse_probe",
                }
            )
            if self._readback_matches(expected_group, observed):
                return
        except Exception as exc:
            # The group may not be open yet, or a transient custom-rendered
            # header may not be OCR-readable.  In both cases the exact search
            # path below is the safe fallback.
            trace.append(
                {
                    "role": "group_header",
                    "component_id": header["component_id"],
                    "phase": "reuse_probe_failed",
                    "error": str(exc),
                }
            )

        for role in _group_open_roles(platform):
            value = executor.execute(components[role], context)
            trace.append(
                {"role": role, "component_id": components[role]["component_id"], "value": value}
            )
            if role == "search_input":
                time.sleep(platform_config["after_search_wait_seconds"])
            elif role == "search_result":
                time.sleep(platform_config["after_open_wait_seconds"])

        observed = str(context.get("group_header") or "")
        if not self._readback_matches(expected_group, observed):
            raise DesktopExecutionError(f"群名回读不一致：{observed or '<空>'}")

    def preflight_readiness(self, platform: str) -> dict[str, Any]:
        """Check read-only group-location controls without enabling sending."""

        normalized = str(platform or "").strip().lower()
        config = self.get_config()
        if normalized not in config["platforms"]:
            return {
                "ready": False,
                "platform": normalized,
                "required_roles": [],
                "reasons": [f"不支持的消息平台：{platform}"],
            }
        platform_config = config["platforms"][normalized]
        roles = _group_open_roles(normalized)
        reasons: list[str] = []
        if not platform_config.get("enabled", True):
            reasons.append(f"{normalized} 平台已禁用")
        if normalized == "yto":
            reasons.extend(YtoServiceChatConfig.from_raw(platform_config).validate())
        for role in roles:
            _, error = self._component_for_role(config, normalized, role)
            if error:
                reasons.append(error)
        return {
            "ready": not reasons,
            "platform": normalized,
            "application": platform_config["application"],
            "required_roles": list(roles),
            "reasons": reasons,
        }

    def preflight_group(self, platform: str, group_name: str) -> ConnectorResult:
        """Open a configured carrier group and read its title without sending.

        This is the validation step used after capture.  It never writes a
        chat message and does not need the global external-write switch.
        """

        normalized = str(platform or "").strip().lower()
        expected_group = str(group_name or "").strip()
        readiness = self.preflight_readiness(normalized)
        if not expected_group:
            return ConnectorResult(False, {"sent": False}, error="群名不能为空")
        if not readiness.get("ready"):
            return ConnectorResult(
                False,
                {"platform": normalized, "group_name": expected_group, "sent": False, "readiness": readiness},
                error="；".join(str(item) for item in readiness.get("reasons") or []),
            )
        config = self.get_config()
        platform_config = config["platforms"][normalized]
        if normalized == "yto":
            writer = YtoServiceChatWriter(
                YtoServiceChatConfig.from_raw(platform_config), self.evidence_dir
            )
            try:
                result = writer.preflight(expected_group)
            except YtoServiceChatError as exc:
                return ConnectorResult(
                    False,
                    {
                        "platform": normalized,
                        "group_name": expected_group,
                        "sent": False,
                    },
                    error=f"圆通 Playwright 只读预检失败：{exc}",
                )
            evidence = str(result.pop("evidence", "") or "")
            return ConnectorResult(
                True,
                result,
                evidence=[evidence] if evidence else [],
            )
        roles = tuple(readiness["required_roles"])
        components: dict[str, dict[str, Any]] = {}
        for role in roles:
            component, error = self._component_for_role(config, normalized, role)
            if error or not component:
                return ConnectorResult(False, {"sent": False}, error=error or "消息组件不可用")
            components[role] = self._role_component(component, role)
        context = {
            "group_name": expected_group,
            "search_group_name": self._search_group_name(expected_group),
        }
        trace: list[dict[str, Any]] = []
        executor = DesktopComponentExecutor(self.evidence_dir)
        try:
            self._open_or_reuse_desktop_chat_group(
                executor=executor,
                components=components,
                context=context,
                expected_group=expected_group,
                platform=normalized,
                platform_config=platform_config,
                trace=trace,
            )
            read_back = str(context.get("group_header") or "")
            if not self._readback_matches(expected_group, read_back):
                raise DesktopExecutionError(f"群名回读不一致：{read_back or '<空>'}")
            evidence_component = components["group_header"]
            evidence_path = self._save_evidence(
                executor,
                evidence_component,
                normalized,
                prefix="message_preflight",
            )
        except Exception as exc:
            return ConnectorResult(
                False,
                {"platform": normalized, "group_name": expected_group, "sent": False, "trace": trace},
                error=f"桌面消息组件只读预检失败：{exc}",
            )
        return ConnectorResult(
            True,
            {
                "platform": normalized,
                "group_name": expected_group,
                "group_header": read_back,
                "sent": False,
                "trace": trace,
            },
            evidence=[evidence_path],
        )

    def send(self, group: dict[str, Any], message: str) -> ConnectorResult:
        platform = str(group.get("platform") or "").strip().lower()
        group_name = str(group.get("group_name") or "").strip()
        if not platform or not group_name or not str(message or "").strip():
            return ConnectorResult(False, {"group": group, "message": message, "sent": False}, error="拦截消息缺少平台、群名或内容")
        readiness = self.platform_readiness(platform)
        if not readiness["ready"]:
            return ConnectorResult(
                False,
                {"group": group, "message": message, "sent": False, "readiness": readiness},
                error="；".join(readiness["reasons"]),
            )
        config = self.get_config()
        platform_config = config["platforms"][platform]
        if platform == "yto":
            writer = YtoServiceChatWriter(
                YtoServiceChatConfig.from_raw(platform_config), self.evidence_dir
            )
            try:
                result = writer.send(group_name, str(message))
            except YtoServiceChatError as exc:
                return ConnectorResult(
                    False,
                    {"group": group, "message": message, "sent": False},
                    error=f"圆通 Playwright 消息发送失败：{exc}",
                )
            evidence = str(result.pop("evidence", "") or "")
            result["group"] = group
            return ConnectorResult(
                True,
                result,
                evidence=[evidence] if evidence else [],
            )
        components: dict[str, dict[str, Any]] = {}
        for role in _required_roles(platform):
            component, error = self._component_for_role(config, platform, role)
            if error or not component:
                return ConnectorResult(False, {"group": group, "message": message, "sent": False}, error=error)
            components[role] = self._role_component(component, role)

        context = {
            "group_name": group_name,
            "search_group_name": self._search_group_name(group_name),
            "message": str(message),
        }
        trace: list[dict[str, Any]] = []
        executor = DesktopComponentExecutor(self.evidence_dir)
        send_clicked = False
        evidence_component = components["send_button"]
        try:
            self._open_or_reuse_desktop_chat_group(
                executor=executor,
                components=components,
                context=context,
                expected_group=group_name,
                platform=platform,
                platform_config=platform_config,
                trace=trace,
            )
            sequence = ("message_input", "send_button")
            for role in sequence:
                value = executor.execute(components[role], context)
                trace.append({"role": role, "component_id": components[role]["component_id"], "value": value})
                if role == "send_button":
                    send_clicked = True
                    time.sleep(platform_config["after_send_wait_seconds"])

            observed_message = self._confirm_desktop_send(
                executor,
                evidence_component,
                platform,
                str(message),
                components["message_input"],
                trace,
            )
            evidence_path = self._save_evidence(executor, evidence_component, platform)
        except Exception as exc:
            failure_evidence: list[str] = []
            if send_clicked:
                try:
                    failure_evidence.append(
                        self._save_evidence(
                            executor,
                            evidence_component,
                            platform,
                            prefix="intercept_unverified",
                        )
                    )
                except Exception:
                    pass
            return ConnectorResult(
                False,
                {
                    "group": group,
                    "message": message,
                    "sent": False,
                    "send_clicked": send_clicked,
                    "delivery_verified": False,
                    "trace": trace,
                },
                evidence=failure_evidence,
                error=f"桌面消息组件执行失败：{exc}",
            )
        return ConnectorResult(
            True,
            {
                "group": group,
                "message": message,
                "sent": True,
                "target_verified": True,
                "read_back": True,
                "sent_echo": observed_message,
                "trace": trace,
            },
            evidence=[evidence_path],
        )
