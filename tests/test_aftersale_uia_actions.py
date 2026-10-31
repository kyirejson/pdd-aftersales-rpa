from __future__ import annotations

from types import SimpleNamespace

import pytest

from pdd_rpa.aftersale_uia import (
    DEFAULT_DIRECT_REJECT_DESCRIPTION,
    PddAftersaleUiaSession,
)
from pdd_rpa.aftersale_workbench import (
    AGREE_RECALL_AFTER_REFUND,
    AGREE_RETURN_AFTER_REFUND,
    AGREE_REFUND,
    REJECT_REFUND,
)


class Rect:
    def __init__(self, left: int, top: int, right: int, bottom: int):
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom

    def width(self) -> int:
        return self.right - self.left

    def height(self) -> int:
        return self.bottom - self.top


class Control:
    """Small UIA-shaped control for action-surface classification tests."""

    def __init__(
        self,
        name: str,
        *,
        control_type: str = "Text",
        rect: tuple[int, int, int, int] = (900, 700, 1200, 740),
        enabled: bool = True,
        visible: bool = True,
        parent=None,
        class_name: str = "",
        runtime_id: tuple[int, ...] | None = None,
        invokable: bool = False,
        value: str = "",
    ):
        self.element_info = SimpleNamespace(
            name=name,
            control_type=control_type,
            class_name=class_name,
            automation_id="",
            runtime_id=runtime_id or (id(self),),
        )
        self._rect = Rect(*rect)
        self._enabled = enabled
        self._visible = visible
        self._parent = parent
        self._value = value
        self.invoke_calls: list[str] = []
        if invokable:
            self.iface_invoke = SimpleNamespace(
                Invoke=lambda: self.invoke_calls.append("invoke")
            )

    def rectangle(self) -> Rect:
        return self._rect

    def is_enabled(self) -> bool:
        return self._enabled

    def is_visible(self) -> bool:
        return self._visible

    def parent(self):
        return self._parent

    def set_edit_text(self, value: str) -> None:
        self._value = value

    def get_value(self) -> str:
        return self._value


def session_with(controls: list[Control]) -> PddAftersaleUiaSession:
    session = object.__new__(PddAftersaleUiaSession)
    session.config = {}
    session._controls = lambda _handle, *args, **kwargs: list(controls)
    return session


def test_action_capabilities_recognize_all_shipped_refund_buttons():
    controls = [
        Control("同意退款", control_type="Button"),
        Control("同意快递退回后退款", control_type="Button"),
        Control("同意召回后退款", control_type="Button"),
        Control("驳回退款", control_type="Button"),
    ]

    result = session_with(controls)._read_action_capabilities(101)

    assert result["action_controls_read_complete"] is True
    assert result["has_agree_refund"] is True
    assert result["has_agree_return_after_refund"] is True
    assert result["has_agree_recall_after_refund"] is True
    assert result["has_reject_refund"] is True


def test_recall_terminal_uses_real_success_marker_without_status_field():
    controls = [
        Control("召回退款操作成功"),
        Control("商家已同意召回后退款，请及时联系物流公司召回商品并处理退款"),
        Control("上传召回凭证", control_type="Button"),
    ]
    result = session_with(controls)._terminal_for_action(
        101, AGREE_RECALL_AFTER_REFUND, baseline_status="待商家处理"
    )

    assert result is not None
    assert result["success_marker"] == "召回退款操作成功"


def test_action_capabilities_normalize_button_whitespace_and_nbsp():
    controls = [
        Control(" 同意\n快递退回后退款 ", control_type="Button"),
        Control("驳回\u00a0退款", control_type="Button"),
    ]

    result = session_with(controls)._read_action_capabilities(101)

    assert result["action_controls_read_complete"] is True
    assert result["has_agree_return_after_refund"] is True
    assert result["has_reject_refund"] is True


def test_action_capabilities_promote_exact_text_child_to_unique_actionable_parent():
    reject_link = Control(
        "",
        control_type="Hyperlink",
        rect=(1180, 630, 1280, 680),
    )
    reject_text = Control(
        "驳回退款",
        control_type="Text",
        rect=(1205, 649, 1254, 664),
        parent=reject_link,
    )
    controls = [
        reject_link,
        reject_text,
        Control(
            "驳回退款：驳回后请与消费者协商",
            control_type="Text",
            rect=(953, 486, 1270, 510),
        ),
    ]

    result = session_with(controls)._read_action_capabilities(101)

    assert result["action_controls_read_complete"] is True
    assert result["available_aftersale_actions"] == [REJECT_REFUND]
    assert result["has_reject_refund"] is True


def test_action_capabilities_accept_real_tight_invokable_group_wrapper(monkeypatch):
    reject_wrapper = Control(
        "",
        control_type="Group",
        rect=(1205, 649, 1254, 665),
        class_name="Beast__3JJF5",
        invokable=True,
    )
    reject_text = Control(
        "驳回退款",
        control_type="Text",
        rect=(1205, 649, 1254, 664),
        parent=reject_wrapper,
    )
    session = session_with([reject_wrapper, reject_text])

    result = session._read_action_capabilities(101)
    targets = session._action_targets(101, "驳回退款")
    monkeypatch.setattr(session, "_focus", lambda _window: None)
    invoked = session._invoke_action_target(
        SimpleNamespace(handle=101), targets[0], "驳回退款"
    )

    assert result["action_controls_read_complete"] is True
    assert result["has_reject_refund"] is True
    assert targets == [reject_wrapper]
    assert reject_wrapper.invoke_calls == ["invoke"]
    assert invoked["method"] == "target_uia_invoke"


def test_action_capabilities_reject_broad_invokable_page_group():
    page_group = Control(
        "",
        control_type="Group",
        rect=(4, 80, 1901, 1643),
        invokable=True,
    )
    reject_text = Control(
        "驳回退款",
        control_type="Text",
        rect=(1205, 649, 1254, 664),
        parent=page_group,
    )

    result = session_with([page_group, reject_text])._read_action_capabilities(101)

    assert result["action_controls_read_complete"] is True
    assert result["has_reject_refund"] is False


def test_action_capabilities_do_not_promote_exact_history_text_under_group():
    history_group = Control(
        "",
        control_type="Group",
        rect=(300, 780, 900, 940),
    )
    history_text = Control(
        "驳回退款",
        control_type="Text",
        rect=(350, 820, 450, 850),
        parent=history_group,
    )

    result = session_with([history_group, history_text])._read_action_capabilities(101)

    assert result["action_controls_read_complete"] is True
    assert result["has_reject_refund"] is False


def test_action_capabilities_deduplicate_label_and_shared_actionable_parent():
    reject_link = Control(
        "驳回退款",
        control_type="Hyperlink",
        rect=(1180, 630, 1280, 680),
    )
    reject_text = Control(
        "驳回退款",
        control_type="Text",
        rect=(1205, 649, 1254, 664),
        parent=reject_link,
    )

    result = session_with([reject_link, reject_text])._read_action_capabilities(101)

    assert result["action_controls_read_complete"] is True
    assert result["has_reject_refund"] is True


def test_action_capabilities_do_not_promote_history_or_help_text_to_actions():
    controls = [
        Control("商家此前已驳回退款", control_type="Text", rect=(300, 300, 650, 330)),
        Control(
            "同意快递退回后退款说明",
            control_type="Text",
            rect=(300, 350, 700, 380),
        ),
        Control("普通退款可点击同意退款处理", control_type="Text"),
    ]

    result = session_with(controls)._read_action_capabilities(101)

    assert result["action_controls_read_complete"] is True
    assert result["has_agree_refund"] is False
    assert result["has_agree_return_after_refund"] is False
    assert result["has_reject_refund"] is False


def test_action_capabilities_report_complete_scan_when_no_action_is_available():
    controls = [
        Control("物流轨迹", control_type="Text"),
        Control("待商家处理", control_type="Text"),
    ]

    result = session_with(controls)._read_action_capabilities(101)

    assert result["action_controls_read_complete"] is True
    assert result["has_agree_refund"] is False
    assert result["has_agree_return_after_refund"] is False
    assert result["has_reject_refund"] is False


def test_duplicate_destructive_action_controls_fail_closed():
    controls = [
        Control("驳回退款", control_type="Button", rect=(900, 700, 1050, 740)),
        Control("驳回退款", control_type="Button", rect=(1100, 700, 1250, 740)),
    ]
    session = session_with(controls)

    try:
        result = session._read_action_capabilities(101)
    except Exception:
        return

    assert result["action_controls_read_complete"] is False
    assert result["has_reject_refund"] is False


def test_classify_reject_surface_keep_reject():
    controls = [
        Control("坚持驳回", control_type="Button"),
        Control("提交", control_type="Button"),
    ]

    assert (
        session_with([])._classify_reject_surface(controls)
        == "KEEP_REJECT"
    )


def test_classify_reject_surface_amount_not_agreed():
    controls = [
        Control("退款金额未达成一致", control_type="RadioButton"),
        Control("提交", control_type="Button"),
    ]

    assert (
        session_with([])._classify_reject_surface(controls)
        == "AMOUNT_NOT_AGREED"
    )


def test_waits_for_progressive_default_selected_amount_form(monkeypatch):
    session = session_with([])
    root = Control(
        "",
        control_type="Group",
        class_name="MDL_inner_amount",
        runtime_id=(42, 3201),
    )
    scope = SimpleNamespace(root_signature=("uia_runtime", 42, 3201))
    loading = [
        Control("退款金额未达成一致", parent=root),
        Control("提交", control_type="Button", parent=root),
    ]
    ready = [
        Control("退款金额未达成一致", parent=root),
        Control(
            "第二步：请与消费者协商退款金额，提交后将自动发送协商话术给消费者。",
            parent=root,
        ),
        Control("提交", control_type="Button", parent=root),
    ]
    snapshots = iter([(scope, loading), (scope, ready)])
    monkeypatch.setattr(
        session,
        "_reject_modal_snapshot",
        lambda _handle: next(snapshots),
    )
    monkeypatch.setattr("pdd_rpa.aftersale_uia.time.sleep", lambda _seconds: None)

    assert session._wait_for_amount_reject_selected_by_semantics(
        101,
        expected_root_signature=scope.root_signature,
        timeout_seconds=0.5,
    ) is True


def test_classify_final_direct_reject_surface_from_real_form():
    controls = [
        Control(
            "消费者描述属实，但不认可消费者提出的售后方案，",
            control_type="RadioButton",
        ),
        Control("驳回并与消费者协商"),
        Control("不协商，直接驳回"),
        Control("驳回描述"),
        Control("请详细描述您向消费者提供的解决方案", control_type="Edit"),
        Control("提交", control_type="Button"),
    ]

    assert (
        session_with([])._classify_reject_surface(controls)
        == "DIRECT_REJECT"
    )


def test_classify_reject_surface_unknown_when_required_fields_are_incomplete():
    controls = [
        Control(
            "消费者描述属实，但不认可消费者提出的售后方案，",
            control_type="RadioButton",
        ),
        Control("提交", control_type="Button"),
    ]

    assert session_with([])._classify_reject_surface(controls) == "UNKNOWN"


def test_classify_keep_reject_negotiation_surface_from_real_dialog_copy():
    controls = [
        Control("请与消费者协商方案"),
        Control("与消费者协商售后方案", control_type="RadioButton"),
        Control("退货后退全款", control_type="CheckBox"),
        Control("提交协商方案", control_type="Button"),
    ]

    assert (
        session_with([])._classify_reject_surface(controls)
        == "KEEP_REJECT_NEGOTIATION"
    )


def test_direct_reject_requires_description_form_not_old_negotiation_choices():
    controls = [
        Control(
            "消费者描述属实，但不认可消费者提出的售后方案，",
            control_type="RadioButton",
        ),
        Control("驳回并与消费者协商", control_type="RadioButton"),
        Control("退货后退全款", control_type="RadioButton"),
        Control("提交", control_type="Button"),
    ]

    assert session_with([])._classify_reject_surface(controls) == "UNKNOWN"


def test_ambiguous_reject_surface_fails_closed():
    controls = [
        Control("坚持驳回", control_type="Button"),
        Control("退款金额未达成一致", control_type="RadioButton"),
        Control("提交", control_type="Button"),
    ]
    session = session_with([])

    try:
        result = session._classify_reject_surface(controls)
    except Exception:
        return

    assert result in {"AMBIGUOUS", "UNKNOWN"}


@pytest.mark.parametrize(
    "message",
    [
        (
            "请耐心等待消费者回复，并协商解决方案； "
            "待倒计时结束，或消费者回复后，您可继续处理驳回"
        ),
        (
            "请耐心等待消费者回复，\n并协商解决方案；\n"
            "待倒计时结束，或消费者回复后，您可继续处理驳回"
        ),
        (
            "请耐心等待消费者回复，并协商解决方案；\u00a0"
            "待倒计时结束，或消费者回复后，您可继续处理驳回"
        ),
    ],
)
def test_detect_reject_cooldown_normalizes_platform_message(message):
    detected = session_with([])._detect_reject_cooldown([Control(message)])

    assert detected


def test_detect_reject_cooldown_when_uia_splits_message_into_controls():
    controls = [
        Control("请耐心等待消费者回复，并协商解决方案；"),
        Control("待倒计时结束，或消费者回复后，您可继续处理驳回"),
    ]

    assert session_with([])._detect_reject_cooldown(controls)


@pytest.mark.parametrize(
    "unrelated",
    [
        "等待买家退货",
        "请耐心等待消费者回复",
        "待倒计时结束",
        "消费者回复后可继续处理",
        "请与消费者协商解决方案",
    ],
)
def test_detect_reject_cooldown_requires_the_complete_platform_semantics(unrelated):
    assert session_with([])._detect_reject_cooldown([Control(unrelated)]) == ""


def test_agree_terminal_accepts_current_refund_success_status_when_button_is_gone():
    controls = [
        Control("售后状态：", control_type="Text", rect=(800, 420, 900, 450)),
        Control("退款成功", control_type="Text", rect=(910, 420, 1050, 450)),
    ]
    session = session_with(controls)

    terminal = session._terminal_for_action(101, AGREE_REFUND)

    assert terminal is not None
    assert terminal["verified"] is True
    assert terminal["success_marker"] == "退款成功"


def test_agree_terminal_does_not_treat_history_copy_as_success_while_action_remains():
    controls = [
        Control("商家已同意退款", control_type="Text"),
        Control("同意退款", control_type="Button"),
    ]
    session = session_with(controls)

    assert session._terminal_for_action(101, AGREE_REFUND) is None


def test_return_refund_terminal_accepts_real_pending_carrier_return_status():
    controls = [
        Control(
            "待快递退回后退款，请商家联系物流拦截快递",
            control_type="Text",
            rect=(960, 430, 1390, 465),
        ),
        # The real post-confirm page still offers direct refund, but no longer
        # offers the already-submitted return-after-refund action.
        Control(
            "同意退款",
            control_type="Button",
            rect=(960, 585, 1045, 615),
        ),
        Control(
            "商家同意快递退回后退款，请商家联系物流拦截快递",
            control_type="Text",
            rect=(960, 920, 1450, 950),
        ),
    ]
    session = session_with(controls)

    terminal = session._terminal_for_action(
        101,
        AGREE_RETURN_AFTER_REFUND,
        baseline_status="待商家处理",
    )

    assert terminal is not None
    assert terminal["verified"] is True
    assert terminal["aftersale_status"] == "待快递退回后退款"
    assert terminal["success_marker"] == "待快递退回后退款"
    assert terminal["action_control_absent"] is True


def test_return_refund_confirmation_is_scoped_to_observed_business_modal():
    modal = Control(
        "",
        control_type="Group",
        rect=(690, 368, 1215, 740),
        class_name="MDL_inner_5-177-0",
    )
    title = Control(
        "确认同意快递退回后退款吗？",
        rect=(835, 388, 1070, 414),
        parent=modal,
    )
    confirm = Control(
        "确认",
        control_type="Button",
        rect=(1049, 692, 1114, 720),
        parent=modal,
        class_name="MDL_okBtn_5-177-0 BTN_primary_5-177-0",
    )
    unrelated = Control(
        "确认",
        control_type="Button",
        # Geometrically covered by the modal but not a real descendant.  UIA
        # can still report such background DOM nodes as visible.
        rect=(820, 650, 885, 678),
    )
    session = session_with([modal, title, confirm, unrelated])

    target = session._confirmation_target(
        101,
        ("确认", "提交"),
        ("同意快递退回后退款", "快递退回后退款"),
    )

    assert target is not None
    assert target[0] is confirm
    assert target[1].rect == (690, 368, 1215, 740)


def test_document_sized_generic_group_is_never_accepted_as_business_modal():
    document = Control(
        "",
        control_type="Group",
        rect=(0, 80, 1920, 1040),
        class_name="Chrome_RenderWidgetHostHWND",
    )
    confirm = Control(
        "确认",
        control_type="Button",
        rect=(1049, 692, 1114, 720),
        parent=document,
    )
    title = Control(
        "确认同意快递退回后退款吗？",
        rect=(835, 388, 1070, 414),
        parent=document,
    )
    session = session_with([document, title, confirm])

    assert (
        session._confirmation_target(
            101,
            ("确认", "提交"),
            ("同意快递退回后退款",),
        )
        is None
    )


def test_reject_cooldown_fragments_in_different_modals_are_not_combined():
    modal_a = Control(
        "",
        control_type="Group",
        rect=(400, 250, 850, 650),
        class_name="MDL_inner_a",
    )
    modal_b = Control(
        "",
        control_type="Group",
        rect=(900, 250, 1350, 650),
        class_name="MDL_inner_b",
    )
    controls = [
        modal_a,
        modal_b,
        Control("请耐心等待消费者回复", parent=modal_a, rect=(450, 350, 800, 390)),
        Control("待倒计时结束", parent=modal_b, rect=(950, 350, 1250, 390)),
        Control("您可继续处理驳回", parent=modal_b, rect=(950, 410, 1250, 450)),
    ]
    session = session_with(controls)

    assert session._reject_modal_snapshot(101) is None


def test_overlapping_modals_with_the_same_rectangle_keep_distinct_root_identity():
    shared_rect = (690, 368, 1215, 740)
    modal_a = Control(
        "",
        control_type="Group",
        rect=shared_rect,
        class_name="MDL_inner_same",
        runtime_id=(42, 1001),
    )
    modal_b = Control(
        "",
        control_type="Group",
        rect=shared_rect,
        class_name="MDL_inner_same",
        runtime_id=(42, 1002),
    )
    controls = [
        modal_a,
        Control("坚持驳回", control_type="Button", parent=modal_a),
        Control("提交", control_type="Button", parent=modal_a),
        modal_b,
        Control("退款金额未达成一致", control_type="RadioButton", parent=modal_b),
        Control("提交", control_type="Button", parent=modal_b),
    ]
    session = session_with(controls)

    # Both are independently valid business surfaces.  They must not be
    # merged by their identical geometry, and ambiguity must fail closed.
    assert session._reject_modal_snapshot(101) is None


def test_old_modal_rectangle_cannot_target_background_submit_after_modal_disappears():
    modal = Control(
        "",
        control_type="Group",
        rect=(690, 368, 1215, 740),
        class_name="MDL_inner_submit",
        runtime_id=(42, 2001),
    )
    modal_submit = Control(
        "提交",
        control_type="Button",
        rect=(1049, 692, 1114, 720),
        parent=modal,
    )
    session = session_with([modal, modal_submit])
    scope = session._dialog_scope_from_anchor(modal_submit, [modal, modal_submit])
    assert scope is not None

    # Chromium can keep the covered background button visible in UIA.  It is
    # inside the old modal rectangle, but it is not a descendant of that root.
    background_submit = Control(
        "提交",
        control_type="Button",
        rect=(1049, 692, 1114, 720),
    )
    session._controls = lambda *_args, **_kwargs: [background_submit]

    with pytest.raises(Exception, match="可点击控件数量：0"):
        session._wait_and_invoke_unique_action(
            SimpleNamespace(handle=101),
            "提交",
            scope=scope,
            submit_attempted=True,
            timeout_seconds=0.1,
        )


def test_replaced_modal_with_same_class_and_rectangle_does_not_match_old_root():
    shared_rect = (690, 368, 1215, 740)
    modal_a = Control(
        "",
        control_type="Group",
        rect=shared_rect,
        class_name="MDL_inner_same",
        runtime_id=(42, 3001),
    )
    modal_b = Control(
        "",
        control_type="Group",
        rect=shared_rect,
        class_name="MDL_inner_same",
        runtime_id=(42, 3002),
    )
    controls_a = [
        modal_a,
        Control("坚持驳回", control_type="Button", parent=modal_a),
        Control("提交", control_type="Button", parent=modal_a),
    ]
    controls_b = [
        modal_b,
        Control("坚持驳回", control_type="Button", parent=modal_b),
        Control("提交", control_type="Button", parent=modal_b),
    ]
    session = session_with(controls_a)
    original = session._reject_modal_snapshot(101)
    assert original is not None
    session._controls = lambda *_args, **_kwargs: controls_b

    assert (
        session._wait_for_reject_modal_snapshot(
            101,
            timeout_seconds=0.1,
            expected_root_signature=original[0].root_signature,
        )
        is None
    )


def test_final_submit_without_strict_modal_scope_fails_closed():
    session = session_with([])

    with pytest.raises(Exception, match="缺少严格业务弹窗范围"):
        session._wait_and_invoke_unique_action(
            SimpleNamespace(handle=101),
            "提交",
            submit_attempted=True,
            scope=None,
        )


def test_current_status_uses_explicit_field_not_history_inside_old_y_band():
    controls = [
        Control("售后状态：", rect=(780, 410, 900, 440)),
        Control("待商家处理", rect=(910, 410, 1050, 440)),
        # This would have won the former page-wide y=75..760 heuristic.
        Control("退款成功", rect=(780, 650, 930, 680)),
    ]
    session = session_with(controls)

    assert session._current_aftersale_status_from_controls(controls) == "待商家处理"


def test_current_status_header_scope_excludes_history_below_action():
    panel = Control(
        "",
        control_type="Group",
        rect=(690, 300, 1215, 620),
        class_name="aftersale_action_panel",
    )
    current_heading = Control(
        "买家已发货，待商家处理",
        rect=(790, 390, 1080, 425),
        parent=panel,
    )
    action = Control(
        "同意退款",
        control_type="Button",
        rect=(950, 530, 1040, 560),
        parent=panel,
    )
    history = Control("退款成功", rect=(790, 650, 930, 680))
    controls = [panel, current_heading, action, history]
    session = session_with(controls)

    assert session._current_aftersale_status_from_controls(controls) == "待商家处理"


def test_current_status_canonicalizes_real_pending_seller_heading():
    panel = Control(
        "",
        control_type="Group",
        rect=(690, 300, 1300, 700),
        class_name="aftersale_action_panel",
    )
    heading = Control(
        "买家申请退款，待卖家处理",
        rect=(790, 390, 1120, 425),
        parent=panel,
    )
    action = Control(
        "同意退款",
        control_type="Button",
        rect=(953, 642, 1036, 670),
        parent=panel,
    )
    controls = [panel, heading, action]
    session = session_with(controls)

    assert session._current_aftersale_status_from_controls(controls) == "待商家处理"


def test_current_status_uses_real_aligned_heading_under_tall_page_group():
    page_group = Control(
        "",
        control_type="Group",
        rect=(332, 136, 1573, 1643),
        class_name="_msfe_main",
    )
    unrelated_left_status = Control(
        "待商家处理",
        rect=(446, 382, 507, 399),
        parent=page_group,
    )
    heading = Control(
        "买家申请退款，待卖家处理",
        rect=(953, 418, 1194, 444),
        parent=page_group,
    )
    action = Control(
        "同意退款",
        control_type="Button",
        rect=(953, 642, 1036, 670),
        parent=page_group,
    )
    history = Control(
        "退款成功",
        rect=(953, 820, 1050, 845),
        parent=page_group,
    )
    controls = [page_group, unrelated_left_status, heading, action, history]
    session = session_with(controls)

    assert session._current_aftersale_status_from_controls(controls) == "待商家处理"


def test_pending_seller_history_without_current_action_does_not_prove_status():
    controls = [
        Control(
            "买家申请退款，待卖家处理",
            rect=(790, 800, 1120, 835),
        )
    ]
    session = session_with(controls)

    assert session._current_aftersale_status_from_controls(controls) == ""


def test_history_only_status_never_proves_current_status():
    controls = [Control("退款成功", rect=(780, 650, 930, 680))]
    session = session_with(controls)

    assert session._current_aftersale_status_from_controls(controls) == ""


def test_radio_button_uses_its_own_selection_pattern_not_parent_invoke(monkeypatch):
    calls: list[str] = []
    parent = Control("选项容器", control_type="Group")
    parent.iface_invoke = SimpleNamespace(Invoke=lambda: calls.append("parent_invoke"))
    radio = Control("退款金额未达成一致", control_type="RadioButton", parent=parent)
    radio.iface_selection_item = SimpleNamespace(Select=lambda: calls.append("select"))
    session = session_with([parent, radio])
    monkeypatch.setattr(session, "_focus", lambda _window: None)

    result = session._invoke_action_target(
        SimpleNamespace(handle=101), radio, "退款金额未达成一致"
    )

    assert calls == ["select"]
    assert result["method"] == "target_selection_item"


def test_terminal_must_be_stable_across_two_snapshots(monkeypatch):
    session = session_with([])
    results = iter(
        [
            {"verified": True, "aftersale_status": "退款成功", "success_marker": "退款成功"},
            None,
            {"verified": True, "aftersale_status": "退款成功", "success_marker": "退款成功"},
            None,
        ]
    )
    monkeypatch.setattr(
        session,
        "_terminal_for_action",
        lambda *_args, **_kwargs: next(results, None),
    )

    terminal = session._wait_for_action_terminal(
        SimpleNamespace(handle=101),
        AGREE_REFUND,
        baseline_status="待商家处理",
        timeout_seconds=1.3,
    )

    assert terminal is None


def test_post_submit_cooldown_accepts_duplicate_order_uia_nodes(monkeypatch):
    order_no = "260000-215774512873832"
    controls = [
        Control(order_no),
        Control(order_no),
        Control("第一轮售后协商"),
        Control("售后协商中，请与消费者进行友好协商"),
        Control("如超过 00 : 29 : 56 后消费者未回复"),
        Control("您可继续处理驳回退款"),
    ]
    session = session_with(controls)
    detail_url = f"https://mms.pinduoduo.com/aftersales-ssr/detail?orderSn={order_no}"
    monkeypatch.setattr(session, "_current_url", lambda _window: detail_url)
    monkeypatch.setattr(session, "_reject_modal_snapshot", lambda _handle: None)
    monkeypatch.setattr(session, "_confirmation_target", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(session, "_terminal_for_action", lambda *_args, **_kwargs: None)

    outcome = session._wait_for_reject_post_submit_outcome(
        SimpleNamespace(handle=101),
        baseline_status="待商家处理",
        order_no=order_no,
        detail_url=detail_url,
        timeout_seconds=0.1,
    )

    assert outcome == {
        "kind": "cooldown",
        "prompt_text": "售后协商中，消费者未回复后可继续处理驳回退款（剩余 00:29:56）",
    }


def test_reject_cooldown_after_opening_dialog_is_deferred_without_submit(monkeypatch):
    session = session_with([])
    session.config = {"aftersale_reject_dialog_wait_seconds": 0.2}
    state = {"opened": False}
    initial = [
        Control("驳回退款", control_type="Button"),
        # Historical timeline text must not be treated as an already-open
        # cooldown dialog; execute still has to click the current top button.
        Control("请耐心等待消费者回复，并协商解决方案；", rect=(300, 850, 800, 880)),
        Control(
            "待倒计时结束，或消费者回复后，您可继续处理驳回",
            rect=(300, 890, 900, 920),
        ),
    ]
    modal = Control(
        "",
        control_type="Group",
        rect=(690, 368, 1215, 740),
        class_name="MDL_inner_5-177-0",
    )
    cooldown = [
        modal,
        Control("请耐心等待消费者回复，并协商解决方案；", parent=modal),
        Control(
            "待倒计时结束，或消费者回复后，您可继续处理驳回",
            parent=modal,
        ),
    ]
    session._controls = lambda _handle, *args, **kwargs: (
        cooldown if state["opened"] else initial
    )
    invoked: list[str] = []

    def invoke(_window, text, **_kwargs):
        invoked.append(text)
        state["opened"] = True
        return ({"text": text, "executed": True}, Control(text, control_type="Button"))

    monkeypatch.setattr(session, "_invoke_unique_action", invoke)
    monkeypatch.setattr(session, "_wait_and_invoke_unique_action", invoke)

    result = session._execute_reject_refund(
        SimpleNamespace(handle=101),
        baseline_status="待商家处理",
        shop={},
        order_no="ORDER-1",
        detail_url="detail-url",
    )

    assert invoked == ["驳回退款"]
    assert result["action"] == REJECT_REFUND
    assert result["deferred"] is True
    assert result["retry_after_seconds"] == 1800
    assert result["business_write_performed"] is False
    assert result["submit_attempted"] is False


def test_active_page_reject_cooldown_is_deferred_without_clicking_reject(monkeypatch):
    controls = [
        Control("驳回退款", control_type="Button"),
        Control("售后协商中，请与消费者进行友好协商"),
        Control("如超过 00 : 28 : 46 后消费者未回复"),
        Control("您可继续处理驳回退款"),
    ]
    session = session_with(controls)
    invoked: list[str] = []

    def invoke(_window, text, **_kwargs):
        invoked.append(text)
        raise AssertionError("冷却中不应再次点击驳回退款")

    monkeypatch.setattr(session, "_invoke_unique_action", invoke)

    result = session._execute_reject_refund(
        SimpleNamespace(handle=101),
        baseline_status="待商家处理",
        shop={},
        order_no="260000-036457165609461",
        detail_url="detail-url",
    )

    assert invoked == []
    assert result["deferred"] is True
    assert result["reason_code"] == "PDD_REJECT_COOLDOWN"
    assert result["retry_after_seconds"] == 1800
    assert "00:28:46" in result["prompt_text"]
    assert result["business_write_performed"] is False
    assert result["submit_attempted"] is False


@pytest.mark.parametrize(
    ("modal_controls", "expected_sequence"),
    [
        (
            [
                Control("退款金额未达成一致", control_type="RadioButton"),
                Control("提交", control_type="Button"),
            ],
            ["驳回退款", "退款金额未达成一致", "提交"],
        ),
    ],
)
def test_reject_state_machine_executes_only_the_classified_sequence(
    monkeypatch, modal_controls, expected_sequence
):
    session = session_with([])
    session.config = {"aftersale_reject_dialog_wait_seconds": 0.2}
    state = {"opened": False}
    initial = [Control("驳回退款", control_type="Button")]
    modal = Control(
        "",
        control_type="Group",
        rect=(690, 368, 1215, 740),
        class_name="MDL_inner_5-177-0",
    )
    for control in modal_controls:
        control._parent = modal
    modal_controls = [modal, *modal_controls]
    session._controls = lambda _handle, *args, **kwargs: (
        modal_controls if state["opened"] else initial
    )
    invoked: list[str] = []

    def invoke(_window, text, **_kwargs):
        invoked.append(text)
        if text == "驳回退款":
            state["opened"] = True
        return ({"text": text, "executed": True}, Control(text, control_type="Button"))

    monkeypatch.setattr(session, "_invoke_unique_action", invoke)
    monkeypatch.setattr(session, "_wait_and_invoke_unique_action", invoke)
    monkeypatch.setattr(session, "_current_url", lambda _window: "detail-url")
    monkeypatch.setattr(session, "assert_page_contract", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        session, "_verify_selected_reject_option", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        session,
        "_wait_for_action_terminal",
        lambda *_args, **_kwargs: {
            "verified": True,
            "aftersale_status": "待买家处理",
        },
    )

    result = session._execute_reject_refund(
        SimpleNamespace(handle=101),
        baseline_status="待商家处理",
        shop={},
        order_no="ORDER-1",
        detail_url="detail-url",
    )

    assert invoked == expected_sequence
    assert result["ok"] is True
    assert result["business_write_performed"] is True
    assert result["submit_attempted"] is True


def test_final_direct_reject_uses_real_non_negotiation_form_and_description(
    monkeypatch,
):
    session = session_with([])
    session.config = {
        "aftersale_reject_dialog_wait_seconds": 0.2,
        "aftersale_direct_reject_form_wait_seconds": 0.2,
    }
    state = {"stage": "top"}
    initial_modal = Control(
        "",
        control_type="Group",
        rect=(690, 230, 1235, 880),
        class_name="MDL_inner_direct_initial",
        runtime_id=(42, 7001),
    )
    form_modal = Control(
        "",
        control_type="Group",
        rect=(690, 230, 1235, 880),
        class_name="MDL_inner_direct_form",
        runtime_id=(42, 7002),
    )
    description_edit = Control(
        "请详细描述您向消费者提供的解决方案、双方未达成一致的原因",
        control_type="Edit",
        parent=form_modal,
    )
    stages = {
        "top": [Control("驳回退款", control_type="Button")],
        "initial": [
            initial_modal,
            Control(
                "消费者描述属实，但不认可消费者提出的售后方案",
                control_type="RadioButton",
                parent=initial_modal,
            ),
            Control("提交", control_type="Button", parent=initial_modal),
        ],
        "form": [
            form_modal,
            Control(
                "消费者描述属实，但不认可消费者提出的售后方案",
                control_type="RadioButton",
                parent=form_modal,
            ),
            Control("驳回并与消费者协商", parent=form_modal),
            Control("不协商，直接驳回", parent=form_modal),
            Control("驳回描述", parent=form_modal),
            description_edit,
            Control("提交", control_type="Button", parent=form_modal),
        ],
    }
    session._controls = lambda *_args, **_kwargs: stages[state["stage"]]
    invoked: list[str] = []

    def invoke_top(_window, text, **_kwargs):
        invoked.append(text)
        if text == "驳回退款":
            state["stage"] = "initial"
        return ({"text": text, "executed": True}, Control(text))

    def invoke_target(_window, _target, text, **kwargs):
        invoked.append(text)
        if "消费者描述属实" in text:
            state["stage"] = "form"
        return {"text": text, "executed": True, **kwargs}

    monkeypatch.setattr(session, "_invoke_unique_action", invoke_top)
    monkeypatch.setattr(session, "_invoke_action_target", invoke_target)
    monkeypatch.setattr(session, "_current_url", lambda _window: "detail-url")
    monkeypatch.setattr(session, "assert_page_contract", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        session,
        "_wait_for_reject_post_submit_outcome",
        lambda *_args, **_kwargs: {
            "kind": "terminal",
            "terminal": {
                "verified": True,
                "aftersale_status": "待买家处理",
                "success_marker": "提交成功",
            },
        },
    )

    result = session._execute_reject_refund(
        SimpleNamespace(handle=101),
        baseline_status="待商家处理",
        shop={},
        order_no="ORDER-1",
        detail_url="detail-url",
    )

    assert invoked == [
        "驳回退款",
        "消费者描述属实，但不认可消费者提出的售后方案",
        "不协商，直接驳回",
        "不协商，直接驳回",
        "提交",
    ]
    assert description_edit.get_value() == DEFAULT_DIRECT_REJECT_DESCRIPTION
    assert result["ok"] is True
    assert result["reject_surface"] == "DIRECT_REJECT"
    assert result["business_write_performed"] is True
    assert result["submit_attempted"] is True


def test_keep_reject_advances_through_optional_negotiation_entry(monkeypatch):
    session = session_with([])
    session.config = {"aftersale_reject_dialog_wait_seconds": 0.2}
    state = {"stage": "top"}

    initial_modal = Control(
        "",
        control_type="Group",
        rect=(690, 368, 1215, 740),
        class_name="MDL_inner_initial",
        runtime_id=(42, 6101),
    )
    entry_modal = Control(
        "",
        control_type="Group",
        rect=(690, 368, 1215, 740),
        class_name="MDL_inner_entry",
        runtime_id=(42, 6102),
    )
    proposal_modal = Control(
        "",
        control_type="Group",
        rect=(670, 245, 1235, 860),
        class_name="MDL_inner_proposal",
        runtime_id=(42, 6103),
    )
    stages = {
        "top": [Control("驳回退款", control_type="Button")],
        "initial": [
            initial_modal,
            Control("坚持驳回", control_type="Button", parent=initial_modal),
            Control("提交", control_type="Button", parent=initial_modal),
        ],
        "entry": [
            entry_modal,
            Control("与消费者协商方案", control_type="Button", parent=entry_modal),
        ],
        "proposal": [
            proposal_modal,
            Control("请与消费者协商方案", parent=proposal_modal),
            Control(
                "与消费者协商售后方案",
                control_type="RadioButton",
                parent=proposal_modal,
            ),
            Control(
                "退货后退全款",
                control_type="CheckBox",
                parent=proposal_modal,
            ),
            Control(
                "提交协商方案",
                control_type="Button",
                parent=proposal_modal,
            ),
        ],
    }
    session._controls = lambda *_args, **_kwargs: stages[state["stage"]]
    invoked: list[str] = []

    def invoke(_window, text, **_kwargs):
        invoked.append(text)
        if text == "驳回退款":
            state["stage"] = "initial"
        elif text == "坚持驳回":
            state["stage"] = "entry"
        elif text == "与消费者协商方案":
            state["stage"] = "proposal"
        return ({"text": text, "executed": True}, Control(text, control_type="Button"))

    def invoke_target(_window, target, text, **_kwargs):
        invoked.append(text)
        return {"text": text, "executed": True, **_kwargs}

    monkeypatch.setattr(session, "_invoke_unique_action", invoke)
    monkeypatch.setattr(session, "_wait_and_invoke_unique_action", invoke)
    monkeypatch.setattr(session, "_invoke_action_target", invoke_target)
    monkeypatch.setattr(
        session,
        "_selection_state",
        lambda target: session._name(target) == "与消费者协商售后方案",
    )
    monkeypatch.setattr(
        session, "_verify_selected_reject_option", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(session, "_current_url", lambda _window: "detail-url")
    monkeypatch.setattr(session, "assert_page_contract", lambda *_args, **_kwargs: {})
    final_target = Control("确认，发给消费者", control_type="Button")
    final_scope = SimpleNamespace(root_signature=("final-confirm",))
    monkeypatch.setattr(
        session,
        "_wait_for_confirmation_target",
        lambda *_args, **_kwargs: (final_target, final_scope),
    )
    monkeypatch.setattr(
        session,
        "_confirmation_target",
        lambda *_args, **_kwargs: (final_target, final_scope),
    )
    monkeypatch.setattr(
        session,
        "_wait_for_reject_post_submit_outcome",
        lambda *_args, **_kwargs: {
            "kind": "cooldown",
            "prompt_text": (
                "请耐心等待消费者回复，并协商解决方案；"
                "待倒计时结束，或消费者回复后，您可继续处理驳回"
            ),
        },
    )

    result = session._execute_reject_refund(
        SimpleNamespace(handle=101),
        baseline_status="待商家处理",
        shop={},
        order_no="ORDER-1",
        detail_url="detail-url",
    )

    assert invoked == [
        "驳回退款",
        "坚持驳回",
        "与消费者协商方案",
        "退货后退全款",
        "提交协商方案",
        "确认，发给消费者",
    ]
    assert result["deferred"] is True
    assert result["reason_code"] == "PDD_REJECT_NEGOTIATION_COOLDOWN"
    assert result["business_write_performed"] is True
    assert result["submit_attempted"] is True
    assert result["write_outcome_unknown"] is False


def test_keep_reject_advances_through_real_go_negotiate_process_modal(monkeypatch):
    session = session_with([])
    session.config = {"aftersale_reject_dialog_wait_seconds": 0.2}
    state = {"stage": "top"}

    initial_modal = Control(
        "",
        control_type="Group",
        rect=(690, 368, 1215, 740),
        class_name="MDL_inner_initial",
        runtime_id=(42, 6201),
    )
    process_modal = Control(
        "",
        control_type="Group",
        rect=(705, 342, 1218, 778),
        class_name="MDL_inner_process",
        runtime_id=(42, 6202),
    )
    proposal_modal = Control(
        "",
        control_type="Group",
        rect=(680, 246, 1240, 862),
        class_name="MDL_inner_proposal",
        runtime_id=(42, 6203),
    )
    stages = {
        "top": [Control("驳回退款", control_type="Button")],
        "initial": [
            initial_modal,
            Control("坚持驳回", control_type="Button", parent=initial_modal),
            Control("提交", control_type="Button", parent=initial_modal),
        ],
        "process": [
            process_modal,
            Control("驳回退款流程", parent=process_modal),
            Control("去协商", control_type="Button", parent=process_modal),
            Control("进一步了解", control_type="Button", parent=process_modal),
        ],
        "proposal": [
            proposal_modal,
            Control("请与消费者协商方案", parent=proposal_modal),
            Control(
                "与消费者协商售后方案",
                control_type="RadioButton",
                parent=proposal_modal,
            ),
            Control("退货后退全款", control_type="CheckBox", parent=proposal_modal),
            Control("提交协商方案", control_type="Button", parent=proposal_modal),
        ],
    }
    session._controls = lambda *_args, **_kwargs: stages[state["stage"]]
    invoked: list[str] = []

    def invoke(_window, text, **_kwargs):
        invoked.append(text)
        if text == "驳回退款":
            state["stage"] = "initial"
        elif text == "坚持驳回":
            state["stage"] = "process"
        elif text == "去协商":
            state["stage"] = "proposal"
        return ({"text": text, "executed": True}, Control(text, control_type="Button"))

    def invoke_target(_window, target, text, **_kwargs):
        invoked.append(text)
        return {"text": text, "executed": True, **_kwargs}

    monkeypatch.setattr(session, "_invoke_unique_action", invoke)
    monkeypatch.setattr(session, "_wait_and_invoke_unique_action", invoke)
    monkeypatch.setattr(session, "_invoke_action_target", invoke_target)
    monkeypatch.setattr(
        session,
        "_selection_state",
        lambda target: session._name(target) == "与消费者协商售后方案",
    )
    monkeypatch.setattr(
        session, "_verify_selected_reject_option", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(session, "_current_url", lambda _window: "detail-url")
    monkeypatch.setattr(session, "assert_page_contract", lambda *_args, **_kwargs: {})
    final_target = Control("确认，发给消费者", control_type="Button")
    final_scope = SimpleNamespace(root_signature=("final-confirm",))
    monkeypatch.setattr(
        session,
        "_wait_for_confirmation_target",
        lambda *_args, **_kwargs: (final_target, final_scope),
    )
    monkeypatch.setattr(
        session,
        "_confirmation_target",
        lambda *_args, **_kwargs: (final_target, final_scope),
    )
    monkeypatch.setattr(
        session,
        "_wait_for_reject_post_submit_outcome",
        lambda *_args, **_kwargs: {
            "kind": "cooldown",
            "prompt_text": "售后协商中，消费者未回复后可继续处理驳回退款",
        },
    )

    result = session._execute_reject_refund(
        SimpleNamespace(handle=101),
        baseline_status="待商家处理",
        shop={},
        order_no="ORDER-1",
        detail_url="detail-url",
    )

    assert invoked == [
        "驳回退款",
        "坚持驳回",
        "去协商",
        "退货后退全款",
        "提交协商方案",
        "确认，发给消费者",
    ]
    assert "进一步了解" not in invoked
    assert result["deferred"] is True
    assert result["stage_committed"] is True
    assert result["business_write_performed"] is True


def test_reject_final_submit_stops_when_modal_root_is_replaced(monkeypatch):
    session = session_with([])
    session.config = {"aftersale_reject_dialog_wait_seconds": 0.2}
    state = {"opened": False, "replaced": False}
    initial = [Control("驳回退款", control_type="Button")]
    shared_rect = (690, 368, 1215, 740)
    modal_a = Control(
        "",
        control_type="Group",
        rect=shared_rect,
        class_name="MDL_inner_same",
        runtime_id=(42, 4001),
    )
    modal_b = Control(
        "",
        control_type="Group",
        rect=shared_rect,
        class_name="MDL_inner_same",
        runtime_id=(42, 4002),
    )

    def modal_controls(root):
        return [
            root,
            Control("退款金额未达成一致", control_type="RadioButton", parent=root),
            Control("提交", control_type="Button", parent=root),
        ]

    controls_a = modal_controls(modal_a)
    controls_b = modal_controls(modal_b)
    session._controls = lambda *_args, **_kwargs: (
        initial
        if not state["opened"]
        else (controls_b if state["replaced"] else controls_a)
    )
    invoked: list[str] = []

    def invoke(_window, text, **_kwargs):
        invoked.append(text)
        if text == "驳回退款":
            state["opened"] = True
        return ({"text": text, "executed": True}, Control(text, control_type="Button"))

    monkeypatch.setattr(session, "_invoke_unique_action", invoke)
    monkeypatch.setattr(session, "_wait_and_invoke_unique_action", invoke)
    monkeypatch.setattr(session, "_current_url", lambda _window: "detail-url")
    monkeypatch.setattr(
        session,
        "assert_page_contract",
        lambda *_args, **_kwargs: state.__setitem__("replaced", True),
    )
    monkeypatch.setattr(
        session, "_verify_selected_reject_option", lambda *_args, **_kwargs: True
    )

    result = session._execute_reject_refund(
        SimpleNamespace(handle=101),
        baseline_status="待商家处理",
        shop={},
        order_no="ORDER-1",
        detail_url="detail-url",
    )

    assert invoked == ["驳回退款", "退款金额未达成一致"]
    assert result["ok"] is False
    assert result["reason_code"] == "PDD_REJECT_MODAL_CHANGED"
    assert result["submit_attempted"] is False


def test_reject_final_submit_rechecks_required_selection(monkeypatch):
    session = session_with([])
    session.config = {"aftersale_reject_dialog_wait_seconds": 0.2}
    state = {"opened": False}
    initial = [Control("驳回退款", control_type="Button")]
    modal = Control(
        "",
        control_type="Group",
        rect=(690, 368, 1215, 740),
        class_name="MDL_inner_amount",
        runtime_id=(42, 5001),
    )
    modal_controls = [
        modal,
        Control("退款金额未达成一致", control_type="RadioButton", parent=modal),
        Control("提交", control_type="Button", parent=modal),
    ]
    session._controls = lambda *_args, **_kwargs: (
        modal_controls if state["opened"] else initial
    )
    invoked: list[str] = []

    def invoke(_window, text, **_kwargs):
        invoked.append(text)
        if text == "驳回退款":
            state["opened"] = True
        return ({"text": text, "executed": True}, Control(text, control_type="Button"))

    selection_reads = iter([True, False])
    monkeypatch.setattr(session, "_invoke_unique_action", invoke)
    monkeypatch.setattr(session, "_wait_and_invoke_unique_action", invoke)
    monkeypatch.setattr(session, "_current_url", lambda _window: "detail-url")
    monkeypatch.setattr(session, "assert_page_contract", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        session,
        "_verify_selected_reject_option",
        lambda *_args, **_kwargs: next(selection_reads),
    )

    result = session._execute_reject_refund(
        SimpleNamespace(handle=101),
        baseline_status="待商家处理",
        shop={},
        order_no="ORDER-1",
        detail_url="detail-url",
    )

    assert invoked == ["驳回退款", "退款金额未达成一致"]
    assert result["ok"] is False
    assert result["reason_code"] == "PDD_REJECT_SELECTION_CHANGED"
    assert result["submit_attempted"] is False


def test_agree_refund_confirmation_clicks_confirm_refund_not_plain_confirm():
    """Regression: the agree-refund modal's primary button reads 确认退款.

    The old sequence clicked an exactly-named ``确认`` control after
    ``同意退款``.  PDD's current confirmation modal labels its primary
    button ``确认退款``， so the exact match timed out on every live
    agree-refund execution.
    """

    session = object.__new__(PddAftersaleUiaSession)
    session.config = {"aftersale_refresh_wait_seconds": 0}
    clicked: list[str] = []
    window = SimpleNamespace(handle=101)
    detail_url = "https://example.invalid/aftersales-ssr/detail?id=1"

    session._current_url = lambda _window: detail_url
    session.assert_page_contract = lambda *_args, **_kwargs: {}
    session._focus = lambda _window: None
    session.keyboard = SimpleNamespace(send_keys=lambda _keys: None)
    session._dismiss_overlay_popups = lambda _window: {}
    session.read_detail = lambda *_args, **_kwargs: {
        "action_controls_read_complete": True,
        "has_agree_refund": True,
    }
    session._controls = lambda _handle, **_kwargs: []
    session._current_aftersale_status_from_controls = lambda _controls: "待商家处理"
    session.read_logistics_detail = lambda *_args, **_kwargs: {
        "logistics_trajectory_count": 2
    }
    session._click_stage = lambda _window, text: clicked.append(text) or {
        "text": text,
        "control_type": "Button",
        "executed": True,
    }

    def fake_terminal(_window, _action, **kwargs):
        if kwargs.get("timeout_seconds") is None:
            return {"aftersale_status": "退款成功", "success_marker": "退款成功"}
        return None

    session._wait_for_action_terminal = fake_terminal

    result = session.execute_action(
        window,
        AGREE_REFUND,
        shop={"id": "shop-1", "name": "测试店"},
        order_no="ORDER-1",
        detail_url=detail_url,
        max_agree_refund_trace_count=3,
    )

    assert clicked == ["同意退款", "确认退款"]
    assert result["ok"] is True
    assert result["business_write_performed"] is True
    assert result["submit_attempted"] is True
