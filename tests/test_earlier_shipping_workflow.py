from pdd_rpa.earlier_shipping_workflow import (
    EARLIER_SHIPPING_OPTION_TEXT,
    EARLIER_SHIPPING_TITLE,
    EarlierShippingComponentWorkflow,
)


class _Repository:
    def __init__(self):
        self.components = {}

    def get_component(self, component_id):
        return self.components.get(component_id)

    def save_component(self, component):
        self.components[component["component_id"]] = component


class _Browser:
    def __init__(self):
        self.submit_visible = False
        self.events = []

    def describe_click_targets(self, _facts, texts):
        result = {}
        for text in texts:
            self.events.append(("describe", text, self.submit_visible))
            visible = text != "提交" or self.submit_visible
            result[text] = {
                "count": 1 if visible else 0,
                "selector_unique": visible,
                "text": text,
                "role": "Button" if text == "提交" else "Text",
                "uia_control_type": "Button" if text == "提交" else "Text",
                "rect": {"x": 10, "y": 20, "width": 100, "height": 20},
            }
        return result

    def execute_click(self, component, _facts, _live):
        text = component["locator"]["text"]
        self.events.append(("click", text, self.submit_visible))
        if text == EARLIER_SHIPPING_OPTION_TEXT:
            self.submit_visible = True
        return {"executed": True, "text": text}

    def wait_for_terminal_state(self, _facts, **kwargs):
        self.events.append(("terminal", kwargs.get("expected_action"), self.submit_visible))
        return {"completed": True, "visible_forms": 0}


def _facts():
    return {
        "order_no": "260000-189606622520042",
        "work_order_title": EARLIER_SHIPPING_TITLE,
    }


def test_submit_is_discovered_only_after_option_click():
    browser = _Browser()
    workflow = EarlierShippingComponentWorkflow(_Repository(), {}, browser=browser)

    result = workflow.execute(_facts(), live=True)

    assert result["ok"] is True
    assert [event[:2] for event in browser.events] == [
        ("describe", EARLIER_SHIPPING_OPTION_TEXT),
        ("click", EARLIER_SHIPPING_OPTION_TEXT),
        ("describe", "提交"),
        ("click", "提交"),
        ("terminal", "CONFIRM_EARLIER_SHIPPING"),
    ]
    assert result["terminal"]["completed"] is True


def test_preview_does_not_search_for_hidden_submit_or_click_option():
    browser = _Browser()
    workflow = EarlierShippingComponentWorkflow(_Repository(), {}, browser=browser)

    result = workflow.execute(_facts(), live=False)

    assert result["submit_deferred_until_selection"] is True
    assert browser.events == [("describe", EARLIER_SHIPPING_OPTION_TEXT, False)]
