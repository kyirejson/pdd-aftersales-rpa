"""Playwright-only writer for the DingTalk interception register.

The table is a browser application.  It is deliberately driven through the
browser's DOM and keyboard APIs instead of UI Automation, screen coordinates,
or OCR.  The signed-in Edge profile must be started with a DevTools endpoint
before the production runner can attach to it.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class DingTalkInterceptionRegisterError(RuntimeError):
    """The shared DingTalk register could not be safely updated."""


DINGTALK_TARGET_JUNFU = "junfu"
DINGTALK_TARGET_SIKU_DISTRIBUTION = "siku_distribution"

_DEFAULT_WAREHOUSE_ALIASES = {
    DINGTALK_TARGET_JUNFU: ("示例自营仓",),
    DINGTALK_TARGET_SIKU_DISTRIBUTION: (
        "示例代发分销",
        "示例供应链代发总仓",
    ),
}


def _normalized_warehouse(value: Any) -> str:
    """Normalize harmless spacing without broad substring matching."""

    return re.sub(r"\s+", "", str(value or "")).strip()


def dingtalk_target_key_for_warehouse(
    raw: dict[str, Any] | None, warehouse: Any
) -> str:
    """Resolve one exact warehouse to its durable online-register target.

    The result is intentionally empty when a warehouse is unknown or appears
    in more than one target.  Sending an interception row to the wrong shared
    document is worse than retaining it for review.
    """

    expected = _normalized_warehouse(warehouse)
    if not expected:
        return ""
    data = dict(raw or {})
    configured = data.get("targets")
    matches: list[str] = []
    if isinstance(configured, dict) and configured:
        items = configured.items()
    else:
        items = ((key, {"warehouse_aliases": aliases}) for key, aliases in _DEFAULT_WAREHOUSE_ALIASES.items())
    for key, target_raw in items:
        target = dict(target_raw or {})
        aliases = target.get("warehouse_aliases") or _DEFAULT_WAREHOUSE_ALIASES.get(
            str(key), ()
        )
        if expected in {_normalized_warehouse(item) for item in aliases}:
            matches.append(str(key))
    return matches[0] if len(matches) == 1 else ""


def resolve_dingtalk_target_config(
    raw: dict[str, Any] | None, target_key: str
) -> dict[str, Any]:
    """Merge common DingTalk settings with one persisted target definition."""

    data = dict(raw or {})
    key = str(target_key or "").strip()
    targets = data.get("targets")
    if isinstance(targets, dict) and targets:
        if key not in targets or not isinstance(targets.get(key), dict):
            raise DingTalkInterceptionRegisterError(
                f"钉钉登记目标“{key or '空'}”未配置，已停止写入"
            )
        merged = {name: value for name, value in data.items() if name != "targets"}
        merged.update(dict(targets[key]))
    elif key == DINGTALK_TARGET_JUNFU:
        # Backward compatibility for migration packages that still contain the
        # original single-table configuration.
        merged = data
    else:
        raise DingTalkInterceptionRegisterError(
            f"钉钉登记目标“{key or '空'}”未配置，已停止写入"
        )
    merged["target_key"] = key
    merged.setdefault("layout", key or DINGTALK_TARGET_JUNFU)
    return merged


def iter_dingtalk_target_configs(
    raw: dict[str, Any] | None,
) -> list[tuple[str, dict[str, Any]]]:
    """Return every configured target using inherited common browser settings."""

    data = dict(raw or {})
    targets = data.get("targets")
    if isinstance(targets, dict) and targets:
        return [
            (str(key), resolve_dingtalk_target_config(data, str(key)))
            for key in targets
        ]
    return [
        (
            DINGTALK_TARGET_JUNFU,
            resolve_dingtalk_target_config(data, DINGTALK_TARGET_JUNFU),
        )
    ]


@dataclass(frozen=True, slots=True)
class DingTalkInterceptionRegisterConfig:
    enabled: bool
    url: str
    edge_profile: str
    document_title: str
    sheet_name: str
    registrar: str
    cdp_endpoint: str
    playwright_user_data_dir: str
    connect_timeout_seconds: float
    action_timeout_seconds: float
    write_wait_seconds: float
    target_key: str
    layout: str

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> "DingTalkInterceptionRegisterConfig":
        data = dict(raw or {})
        target_key = str(data.get("target_key") or DINGTALK_TARGET_JUNFU).strip()
        layout = str(data.get("layout") or target_key).strip()
        if layout not in {
            DINGTALK_TARGET_JUNFU,
            DINGTALK_TARGET_SIKU_DISTRIBUTION,
        }:
            raise DingTalkInterceptionRegisterError(
                f"不支持的钉钉登记列布局：{layout or '空'}"
            )
        return cls(
            enabled=bool(data.get("enabled", False)),
            url=str(data.get("url") or "").strip(),
            edge_profile=str(data.get("edge_profile") or "Profile 19").strip(),
            document_title=str(data.get("document_title") or "").strip(),
            sheet_name=str(data.get("sheet_name") or "2026").strip(),
            registrar=str(data.get("registrar") or "rpa").strip() or "rpa",
            cdp_endpoint=str(data.get("cdp_endpoint") or "http://127.0.0.1:9222").rstrip("/"),
            playwright_user_data_dir=str(data.get("playwright_user_data_dir") or "").strip(),
            connect_timeout_seconds=max(3.0, float(data.get("connect_timeout_seconds", 15))),
            action_timeout_seconds=max(2.0, float(data.get("action_timeout_seconds", 12))),
            write_wait_seconds=max(0.1, float(data.get("write_wait_seconds", 0.45))),
            target_key=target_key,
            layout=layout,
        )


def format_dingtalk_date(value: str | date) -> str:
    """Use the ledger's date format without zero-padding month/day."""
    if isinstance(value, date):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    return f"{parsed.year}/{parsed.month}/{parsed.day}"


def dingtalk_row_values(
    record: dict[str, Any],
    registrar: str,
    *,
    written_on: str | date | None = None,
) -> dict[str, str]:
    """Build semantic values; the selected target maps them to physical columns."""
    amount = float(record.get("amount") or 0)
    return {
        "date": format_dingtalk_date(written_on or date.today()),
        "registrar": str(registrar or "rpa").strip() or "rpa",
        "group_name": str(record.get("group_name") or "").strip(),
        "tracking_no": str(record.get("tracking_no") or "").strip(),
        "order_no": str(record.get("order_no") or "").strip(),
        "amount": f"{amount:.2f}",
    }


class DingTalkInterceptionRegisterWriter:
    """Append one record through a connected Playwright Edge session only."""

    # The live page has used FlexTableWrapper.  Keep semantic DOM selectors in
    # one place so a future DingTalk DOM change is a safe, visible failure.
    _GRID_SELECTORS = (
        '[data-automation-id="FlexTableWrapper"]',
        '[data-testid="FlexTableWrapper"]',
        "#FlexTableWrapper",
        ".FlexTableWrapper",
        '[role="grid"]',
    )
    _CELL_NAME_RE = re.compile(r"^[A-Z]{1,4}(\d+)$")
    _LAYOUTS = {
        DINGTALK_TARGET_JUNFU: {
            "tracking_column": "C",
            "append_reference_columns": ("A", "C", "E"),
            "reference_range_end": "E",
            "reference_offsets": (0, 2, 4),
            "occupied_columns": "ABCEGH",
            "written_columns": ("A", "B", "C", "E", "G"),
            "skipped_columns": ("D", "F"),
            "duplicate_note_column": "H",
        },
        DINGTALK_TARGET_SIKU_DISTRIBUTION: {
            "tracking_column": "F",
            "append_reference_columns": ("A", "E", "F", "H", "J"),
            "reference_range_end": "J",
            "reference_offsets": (0, 4, 5, 7, 9),
            "occupied_columns": "ABEFHJ",
            "written_columns": ("A", "B", "E", "F", "H", "J"),
            "skipped_columns": ("C", "D", "G", "I"),
            "duplicate_note_column": "",
        },
    }

    def __init__(
        self,
        config: DingTalkInterceptionRegisterConfig,
        evidence_dir: Path,
    ) -> None:
        self.config = config
        self.evidence_dir = Path(evidence_dir) / "dingtalk_interception_register"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _layout(self) -> dict[str, Any]:
        try:
            return self._LAYOUTS[self.config.layout]
        except KeyError as exc:
            raise DingTalkInterceptionRegisterError(
                f"不支持的钉钉登记列布局：{self.config.layout}"
            ) from exc

    def open_window(self) -> dict[str, Any]:
        """Open or reuse the isolated DingTalk document window only.

        This is intentionally separate from :meth:`append`: opening the
        window must never inspect rows or write to the online sheet.
        """
        if not self.config.enabled:
            raise DingTalkInterceptionRegisterError("钉钉在线表格同步未启用")
        playwright, browser = self._connect()
        try:
            target = None
            expected_path = self._document_url_key(self.config.url)
            pages = [
                page
                for context in browser.contexts
                for page in context.pages
            ]
            reused = False
            for page in pages:
                page_url = str(page.url or "")
                if expected_path and expected_path in page_url:
                    target = page
                    reused = True
                    break
            if target is None:
                contexts = list(browser.contexts)
                if not contexts:
                    raise DingTalkInterceptionRegisterError(
                        "钉钉独立配置没有可用浏览器上下文"
                    )
                target = contexts[0].new_page()
                target.goto(self.config.url, wait_until="domcontentloaded")
            current_url = str(target.url or "")
            if not expected_path or expected_path not in current_url:
                raise DingTalkInterceptionRegisterError("钉钉独立窗口未打开指定在线表格")
            target.bring_to_front()
            return {
                "ok": True,
                "action": "reuse" if reused else "open",
                "opened": not reused,
                "verified": True,
                "profile": self.config.edge_profile,
                "url": current_url,
                "write_performed": False,
            }
        finally:
            # Releasing Playwright must not close the attached Edge window.
            playwright.stop()

    def append(self, record: dict[str, Any], *, local_duplicate_tracking: bool) -> dict[str, Any]:
        if not self.config.enabled:
            raise DingTalkInterceptionRegisterError("钉钉快递拦截登记同步未启用")
        values = dingtalk_row_values(record, self.config.registrar)
        if not values["tracking_no"]:
            raise DingTalkInterceptionRegisterError("快递拦截登记缺少快递单号，已停止写入")
        if not values["order_no"]:
            raise DingTalkInterceptionRegisterError("快递拦截登记缺少订单编号，已停止写入")
        if (
            self.config.layout == DINGTALK_TARGET_SIKU_DISTRIBUTION
            and not values["group_name"]
        ):
            raise DingTalkInterceptionRegisterError(
                "示例代发分销登记缺少拦截群名称，已停止写入"
            )

        playwright, browser = self._connect()
        try:
            page = self._find_document_page(browser)
            page.set_default_timeout(int(self.config.action_timeout_seconds * 1000))
            page.bring_to_front()
            self._grant_clipboard(page)
            sheet = self._find_sheet_frame(page)
            self._select_sheet(sheet)
            grid = self._find_grid(sheet)

            # The online sheet is edited by people as well as the RPA.  Check
            # every populated C-column cell before append; virtualised visible
            # DOM text alone is not a complete duplicate audit.
            # Human-maintained rows do not always contain the date (A) or
            # registrar (B).  Treat the furthest populated date (A), tracking-
            # number (C), or original-order (E) cell as the append boundary so
            # those rows can never be overwritten or hidden from the duplicate
            # audit.
            last_row = self._find_last_reference_row(page, sheet, grid)
            online_duplicate = self._online_duplicate_tracking(
                page, sheet, grid, values["tracking_no"], last_row
            )
            duplicate_tracking = bool(local_duplicate_tracking or online_duplicate)
            remote_row = self._append_after_last_reference(
                page, sheet, grid, values, duplicate_tracking, last_row=last_row
            )
            evidence = self._save_evidence(page, f"written_row_{remote_row}")
            return {
                "ok": True,
                "remote_row": remote_row,
                "tracking_no": values["tracking_no"],
                "duplicate_tracking": duplicate_tracking,
                "duplicate_sources": {
                    "local": bool(local_duplicate_tracking),
                    "online_full_column": bool(online_duplicate),
                },
                "evidence": str(evidence),
                "target_key": self.config.target_key,
                "layout": self.config.layout,
                "written_columns": list(self._layout["written_columns"])
                + (
                    [str(self._layout["duplicate_note_column"])]
                    if duplicate_tracking and self._layout["duplicate_note_column"]
                    else []
                ),
                "skipped_columns": list(self._layout["skipped_columns"]),
                "append_reference_columns": list(
                    self._layout["append_reference_columns"]
                ),
            }
        except DingTalkInterceptionRegisterError:
            raise
        except Exception as exc:
            raise DingTalkInterceptionRegisterError(f"钉钉在线表格写入失败：{exc}") from exc
        finally:
            # ``connect_over_cdp`` attaches to the user's existing Edge.
            # Closing ``browser`` here can close every Profile window, so only
            # release our Playwright client connection.
            playwright.stop()

    def _connect(self) -> tuple[Any, Any]:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise DingTalkInterceptionRegisterError(
                "缺少 Playwright；请按 requirements.txt 安装依赖"
            ) from exc
        playwright = sync_playwright().start()
        try:
            browser = playwright.chromium.connect_over_cdp(
                self.config.cdp_endpoint,
                timeout=int(self.config.connect_timeout_seconds * 1000),
            )
        except PlaywrightError as exc:
            if not self.config.playwright_user_data_dir:
                playwright.stop()
                raise DingTalkInterceptionRegisterError(
                    f"无法连接钉钉 Playwright 配置“{self.config.edge_profile}”。请使用带 "
                    f"--remote-debugging-port={urlsplit(self.config.cdp_endpoint).port or 9222} "
                    "的方式启动该配置后重试。"
                ) from exc
            self._launch_playwright_edge()
            try:
                deadline = time.monotonic() + self.config.connect_timeout_seconds
                while True:
                    try:
                        browser = playwright.chromium.connect_over_cdp(
                            self.config.cdp_endpoint, timeout=1_500
                        )
                        break
                    except PlaywrightError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.35)
            except PlaywrightError as retry_exc:
                playwright.stop()
                raise DingTalkInterceptionRegisterError(
                    f"无法启动或连接钉钉 Playwright 配置“{self.config.edge_profile}”"
                ) from retry_exc
        return playwright, browser

    def _launch_playwright_edge(self) -> None:
        root = Path(self.config.playwright_user_data_dir).expanduser()
        if not root.is_absolute():
            root = (Path.cwd() / root).resolve()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DingTalkInterceptionRegisterError("无法创建钉钉 Playwright 配置目录") from exc
        port = urlsplit(self.config.cdp_endpoint).port or 9222
        edge_candidates = (
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        )
        edge = next((candidate for candidate in edge_candidates if candidate.is_file()), None)
        if edge is None:
            raise DingTalkInterceptionRegisterError("未找到 Microsoft Edge，无法启动钉钉 Playwright 配置")
        try:
            subprocess.Popen(
                [
                    str(edge),
                    f"--user-data-dir={root}",
                    f"--remote-debugging-port={port}",
                    "--remote-debugging-address=127.0.0.1",
                    "--remote-allow-origins=*",
                    "--new-window",
                    self.config.url,
                ],
                cwd=str(edge.parent),
            )
        except OSError as exc:
            raise DingTalkInterceptionRegisterError("无法启动钉钉 Playwright 浏览器") from exc

    def _find_document_page(self, browser: Any) -> Any:
        expected_url = self._document_url_key(self.config.url)
        candidates: list[Any] = []
        for context in browser.contexts:
            for page in context.pages:
                url_match = expected_url and expected_url in str(page.url or "")
                title_match = False
                try:
                    title_match = bool(
                        self.config.document_title
                        and self.config.document_title in str(page.title() or "")
                    )
                except Exception:
                    pass
                if url_match or title_match:
                    candidates.append(page)
        if len(candidates) != 1:
            raise DingTalkInterceptionRegisterError(
                f"钉钉登记表页面不唯一：{len(candidates)}；已停止写入"
            )
        return candidates[0]

    @staticmethod
    def _find_sheet_frame(page: Any) -> Any:
        """Return the one DOM frame that owns the live spreadsheet canvas."""
        frames = [
            frame
            for frame in page.frames
            if "/spreadsheetv2/" in str(getattr(frame, "url", "") or "")
        ]
        if len(frames) != 1:
            raise DingTalkInterceptionRegisterError(
                f"钉钉在线表格 iframe 不唯一：{len(frames)}；已停止写入"
            )
        return frames[0]

    @staticmethod
    def _document_url_key(value: str) -> str:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return str(value or "")
        return parsed.path.rstrip("/")

    def _grant_clipboard(self, page: Any) -> None:
        parsed = urlsplit(str(page.url or self.config.url))
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else None
        try:
            page.context.grant_permissions(
                ["clipboard-read", "clipboard-write"], origin=origin
            )
        except Exception as exc:
            raise DingTalkInterceptionRegisterError("无法授予钉钉表格剪贴板权限") from exc

    def _select_sheet(self, sheet: Any) -> None:
        matches = sheet.locator(
            '[data-testid="dingdocs-sheet-bar-SheetText"]'
        ).filter(has_text=re.compile(f"^{re.escape(self.config.sheet_name)}$"))
        if matches.count() != 1:
            raise DingTalkInterceptionRegisterError(
                f"钉钉表格页签“{self.config.sheet_name}”不唯一：{matches.count()}"
            )
        matches.click()

    def _find_grid(self, sheet: Any) -> Any:
        for selector in self._GRID_SELECTORS:
            candidate = sheet.locator(selector)
            if candidate.count() == 1 and candidate.is_visible():
                return candidate
        raise DingTalkInterceptionRegisterError("未识别到钉钉在线表格网格 DOM，已停止写入")

    def _online_duplicate_tracking(
        self, page: Any, sheet: Any, grid: Any, tracking_no: str, last_row: int
    ) -> bool:
        """Read the whole populated tracking-number column through Playwright.

        DingTalk renders cells on a canvas, so ``get_by_text`` only sees a
        small virtualised window.  Selecting C1:C{last_row} with the table's
        DOM address control and reading its clipboard value gives a complete,
        exact, non-OCR duplicate check.
        """
        try:
            cells = self._copy_range(
                page,
                sheet,
                grid,
                f"{self._layout['tracking_column']}1:"
                f"{self._layout['tracking_column']}{last_row}",
                "扫描钉钉发货快递单号列",
            )
            return self._contains_tracking(cells, tracking_no)
        except Exception as exc:
            if isinstance(exc, DingTalkInterceptionRegisterError):
                raise
            raise DingTalkInterceptionRegisterError("无法完整读取钉钉发货快递单号列") from exc

    @staticmethod
    def _contains_tracking(cells: list[str], tracking_no: str) -> bool:
        expected = str(tracking_no or "").strip()
        return bool(expected and any(str(cell or "").strip() == expected for cell in cells))

    def _append_after_last_reference(
        self,
        page: Any,
        sheet: Any,
        grid: Any,
        values: dict[str, str],
        duplicate_tracking: bool,
        *,
        last_row: int,
    ) -> int:
        if self.config.layout == DINGTALK_TARGET_SIKU_DISTRIBUTION:
            return self._append_siku_distribution(
                page,
                sheet,
                grid,
                values,
                last_row=last_row,
            )
        target_row = last_row + 1
        self._select_address(page, sheet, f"A{target_row}")
        # F is allowed to contain a preconfigured dropdown label and D can
        # contain a default value.  Any other data means a human already owns
        # this row, so refuse rather than overwrite it.
        occupied = self._read_cells(page, sheet, grid, target_row, "ABCEGH")
        if any(occupied.values()):
            raise DingTalkInterceptionRegisterError(
                f"钉钉新增行 {target_row} 已有数据，已停止写入：{occupied}"
            )
        self._select_address(page, sheet, f"A{target_row}")

        self._paste_and_tab(page, values["date"], "写入日期")
        self._paste_and_tab(page, values["registrar"], "写入登记员")
        self._paste_current_cell(page, values["tracking_no"], "写入快递单号")
        self._assert_current_cell(page, values["tracking_no"], "校验快递单号")
        # D is the carrier column, but it is now maintained manually in the
        # shared sheet.  Move over it without writing anything.
        self._press(page, "Tab", "进入快递公司列")
        self._press(page, "Tab", "跳过快递公司列")
        self._paste_and_tab(page, values["order_no"], "写入原始订单号")
        # F is a user-maintained dropdown and must never be touched.
        self._press(page, "Tab", "跳过退款原因列")
        self._paste_current_cell(page, values["amount"], "写入金额")
        self._assert_current_cell(page, values["amount"], "校验金额")
        if duplicate_tracking:
            self._press(page, "Tab", "进入备注列")
            self._paste_current_cell(page, "重复", "标记重复单号")
            self._assert_current_cell(page, "重复", "校验重复备注")
        return target_row

    def _append_siku_distribution(
        self,
        page: Any,
        sheet: Any,
        grid: Any,
        values: dict[str, str],
        *,
        last_row: int,
    ) -> int:
        """Append only A/B/E/F/H/J in the 示例代发分销 ledger."""

        target_row = last_row + 1
        self._select_address(page, sheet, f"A{target_row}")
        occupied = self._read_cells(
            page,
            sheet,
            grid,
            target_row,
            str(self._layout["occupied_columns"]),
        )
        if any(occupied.values()):
            raise DingTalkInterceptionRegisterError(
                f"钉钉新增行 {target_row} 已有数据，已停止写入：{occupied}"
            )
        self._select_address(page, sheet, f"A{target_row}")
        self._paste_and_tab(page, values["date"], "写入日期")
        self._paste_current_cell(page, values["registrar"], "写入登记员")
        self._assert_current_cell(page, values["registrar"], "校验登记员")

        # Address selection is deterministic and prevents skipped/user-owned
        # columns from being changed by an accidental extra Tab.
        for column, value, label in (
            ("E", values["group_name"], "拦截群名称"),
            ("F", values["tracking_no"], "快递单号"),
            ("H", values["order_no"], "订单号"),
            ("J", values["amount"], "金额"),
        ):
            self._select_address(page, sheet, f"{column}{target_row}")
            self._paste_current_cell(page, value, f"写入{label}")
            self._assert_current_cell(page, value, f"校验{label}")
        return target_row

    @staticmethod
    def _focus_grid(grid: Any) -> None:
        try:
            grid.focus()
        except Exception as exc:
            raise DingTalkInterceptionRegisterError("无法将键盘焦点放入钉钉表格网格") from exc

    @staticmethod
    def _visible_locator_items(locator: Any) -> list[Any]:
        """Expand a Playwright locator without triggering strict-mode clicks."""

        try:
            count = locator.count()
            items = [locator] if count == 1 else [locator.nth(index) for index in range(count)]
            visible: list[Any] = []
            for item in items:
                try:
                    if item.is_visible():
                        visible.append(item)
                except AttributeError:
                    # Keeps small locator-compatible test doubles usable; real
                    # Playwright locators always expose ``is_visible``.
                    visible.append(item)
            return visible
        except Exception:
            return []

    def _selected_cell(self, sheet: Any) -> str:
        selection = sheet.locator('[data-testid="SelectionDisplayContainer"]')
        count = selection.count()
        if count < 1:
            raise DingTalkInterceptionRegisterError(
                "未找到钉钉单元格地址 DOM"
            )
        candidates = self._visible_locator_items(selection)
        values: list[str] = []
        for candidate in candidates:
            try:
                value = str(candidate.inner_text() or "").strip().upper()
            except Exception:
                continue
            if self._CELL_NAME_RE.fullmatch(value):
                values.append(value)
        unique_values = list(dict.fromkeys(values))
        # DingTalk sometimes keeps two visible accessibility nodes for the
        # same selection address.  Equal values are one deterministic state;
        # differing addresses remain unsafe and are rejected.
        if len(unique_values) != 1:
            raise DingTalkInterceptionRegisterError(
                f"钉钉单元格地址 DOM 不唯一：{count}；可见地址={unique_values}"
            )
        return unique_values[0]

    def _selected_row(self, sheet: Any) -> int:
        match = self._CELL_NAME_RE.fullmatch(self._selected_cell(sheet))
        assert match is not None
        return int(match.group(1))

    def _select_address(self, page: Any, sheet: Any, address: str) -> None:
        display = sheet.locator('[data-testid="SelectionDisplayContainer"]')
        editor = sheet.locator('[data-testid="SelectionEditContainer"]')
        for _ in range(3):
            visible_editors = self._visible_locator_items(editor)
            if len(visible_editors) != 1:
                displays = self._visible_locator_items(display)
                if not displays:
                    raise DingTalkInterceptionRegisterError(
                        "无法定位可见的钉钉单元格地址控件"
                    )
                displays[0].click(force=True)
                page.wait_for_timeout(100)
                editor = sheet.locator('[data-testid="SelectionEditContainer"]')
                visible_editors = self._visible_locator_items(editor)
            if len(visible_editors) == 1:
                visible_editors[0].fill(address)
                visible_editors[0].press("Enter")
                page.wait_for_timeout(120)
                if self._selected_cell(sheet) == address.split(":", 1)[0].upper():
                    return
            self._press(page, "Escape", "关闭单元格地址输入")
        raise DingTalkInterceptionRegisterError(
            f"无法通过钉钉 DOM 定位单元格：{address}"
        )

    def _read_cells(
        self, page: Any, sheet: Any, grid: Any, row: int, columns: str
    ) -> dict[str, str]:
        values: dict[str, str] = {}
        for column in columns:
            self._select_address(page, sheet, f"{column}{row}")
            self._focus_grid(grid)
            self._press(page, "Control+C", "读取钉钉单元格")
            page.wait_for_timeout(80)
            values[column] = str(
                page.evaluate("() => navigator.clipboard.readText()") or ""
            ).strip()
        return values

    def _copy_range(
        self, page: Any, sheet: Any, grid: Any, address: str, stage: str
    ) -> list[str]:
        self._select_address(page, sheet, address)
        self._focus_grid(grid)
        self._press(page, "Control+C", stage)
        page.wait_for_timeout(max(200, int(self.config.write_wait_seconds * 1000)))
        raw = str(page.evaluate("() => navigator.clipboard.readText()") or "")
        return raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    def _find_last_reference_row(self, page: Any, sheet: Any, grid: Any) -> int:
        """Find the last row occupied in this target's reference columns.

        DingTalk accepts rectangular selections, so the copied A:E range also
        contains B and D.  They are deliberately ignored: the registrar and
        manually maintained carrier are not append-boundary identities.
        """
        self._focus_grid(grid)
        self._press(page, "Control+End", "读取钉钉表格最大行")
        upper_row = self._selected_row(sheet)
        if upper_row < 1:
            raise DingTalkInterceptionRegisterError("钉钉表格没有可追加的数据行")
        batch_size = 250
        for high in range(upper_row, 0, -batch_size):
            low = max(1, high - batch_size + 1)
            rows = self._copy_range(
                page,
                sheet,
                grid,
                f"A{low}:{self._layout['reference_range_end']}{high}",
                "读取钉钉登记目标列",
            )
            offset = self._last_reference_offset(
                rows,
                high - low + 1,
                tuple(self._layout["reference_offsets"]),
            )
            if offset is not None:
                return low + offset
        raise DingTalkInterceptionRegisterError(
            "未在钉钉表格目标列找到表头或已登记数据"
        )

    @staticmethod
    def _last_reference_offset(
        rows: list[str],
        expected_rows: int,
        reference_offsets: tuple[int, ...] = (0, 2, 4),
    ) -> int | None:
        """Return the last copied row with a configured reference value."""

        usable = min(len(rows), max(0, int(expected_rows)))
        for offset in range(usable - 1, -1, -1):
            columns = str(rows[offset] or "").split("\t")
            if any(
                columns[index].strip()
                for index in reference_offsets
                if index < len(columns)
            ):
                return offset
        return None

    def _paste_and_tab(self, page: Any, value: str, stage: str) -> None:
        self._paste_current_cell(page, value, stage)
        self._press(page, "Tab", f"{stage}后进入下一列")

    def _paste_current_cell(self, page: Any, value: str, stage: str) -> None:
        try:
            page.evaluate("value => navigator.clipboard.writeText(value)", str(value))
            self._press(page, "Control+V", stage)
            page.wait_for_timeout(int(self.config.write_wait_seconds * 1000))
        except DingTalkInterceptionRegisterError:
            raise
        except Exception as exc:
            raise DingTalkInterceptionRegisterError(f"钉钉表格{stage}失败：{exc}") from exc

    def _assert_current_cell(self, page: Any, expected: str, stage: str) -> None:
        try:
            self._press(page, "Control+C", stage)
            page.wait_for_timeout(120)
            actual = str(page.evaluate("() => navigator.clipboard.readText()") or "").strip()
        except DingTalkInterceptionRegisterError:
            raise
        except Exception as exc:
            raise DingTalkInterceptionRegisterError(f"钉钉表格{stage}失败：{exc}") from exc
        if actual != str(expected):
            raise DingTalkInterceptionRegisterError(
                f"钉钉单元格回读不一致：期望 {expected}，实际 {actual or '空'}"
            )

    @staticmethod
    def _press(page: Any, key: str, stage: str) -> None:
        try:
            page.keyboard.press(key)
        except Exception as exc:
            raise DingTalkInterceptionRegisterError(f"钉钉表格{stage}失败：{exc}") from exc

    def _save_evidence(self, page: Any, label: str) -> Path:
        path = self.evidence_dir / f"{label}_{int(time.time())}.png"
        page.screenshot(path=str(path), full_page=False)
        return path


__all__ = [
    "DINGTALK_TARGET_JUNFU",
    "DINGTALK_TARGET_SIKU_DISTRIBUTION",
    "DingTalkInterceptionRegisterConfig",
    "DingTalkInterceptionRegisterError",
    "DingTalkInterceptionRegisterWriter",
    "dingtalk_row_values",
    "dingtalk_target_key_for_warehouse",
    "format_dingtalk_date",
    "iter_dingtalk_target_configs",
    "resolve_dingtalk_target_config",
]
