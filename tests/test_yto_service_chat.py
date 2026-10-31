from pathlib import Path

from pdd_rpa.yto_service_chat import YtoServiceChatConfig, YtoServiceChatWriter
from pdd_rpa.yto_credentials import YtoCredentials


class _Collection:
    def __init__(self, items):
        self.items = list(items)

    def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class _Button:
    def __init__(self, checkbox=None):
        self.click_count = 0
        self.scrolled = False
        self.checkbox = checkbox

    def is_visible(self):
        return True

    def is_enabled(self):
        return self.checkbox is None or self.checkbox.checked

    def scroll_into_view_if_needed(self):
        self.scrolled = True

    def click(self):
        self.click_count += 1


class _Checkbox:
    def __init__(self, checked=False):
        self.checked = checked

    def is_visible(self):
        return True

    def is_checked(self):
        return self.checked

    def check(self):
        self.checked = True


class _Password:
    def __init__(self, value="saved-value"):
        self.keys = []
        self.value = value
        self.fills = []
        self.events = []
        self.clicks = 0
        self.sequential = []

    def input_value(self):
        return self.value

    def fill(self, value):
        self.fills.append(value)
        self.value = value

    def click(self):
        self.clicks += 1

    def press_sequentially(self, value, *, delay):
        self.sequential.append((value, delay))
        self.value = value

    def dispatch_event(self, value):
        self.events.append(value)

    def press(self, value):
        self.keys.append(value)


class _Inputs:
    def __init__(self, username, password):
        self.username = username
        self.password = password

    def count(self):
        return 2

    def nth(self, index):
        return self.username if index == 0 else self.password


class _Page:
    url = "https://kh.yto.net.cn/new/login"

    def __init__(self, button, checkbox):
        self.button = button
        self.checkbox = checkbox
        self.waits = []

    def locator(self, selector):
        if selector == 'input[type="checkbox"]':
            return _Collection([self.checkbox])
        return _Collection([self.button])

    def wait_for_timeout(self, value):
        self.waits.append(value)


class _RefreshPage:
    def __init__(self):
        self.reloads = []
        self.waits = []

    def reload(self, **kwargs):
        self.reloads.append(kwargs)

    def wait_for_timeout(self, value):
        self.waits.append(value)


class _TextItem:
    def __init__(self, box, visible=True):
        self.box = box
        self.visible = visible

    def is_visible(self):
        return self.visible

    def bounding_box(self):
        return self.box


class _TextScope:
    def __init__(self, items):
        self.items = items

    def get_by_text(self, _text, exact=True):
        assert exact is True
        return _Collection(self.items)


class _EditorBox:
    def bounding_box(self):
        return {"x": 100, "y": 600, "width": 500, "height": 100}


def _writer():
    return YtoServiceChatWriter(
        YtoServiceChatConfig.from_raw({"expected_group_name": "示例客户服务群"}),
        Path("runtime/evidence"),
    )


def test_yto_login_clicks_real_button_and_checks_privacy(monkeypatch):
    checkbox = _Checkbox(False)
    button = _Button(checkbox)
    username = _Password("user-sentinel")
    password = _Password("pass-sentinel")
    page = _Page(button, checkbox)
    writer = _writer()
    writer._scopes = lambda _page: [page]
    writer._wait_for_login_success = lambda _page, _seconds: True

    trace = writer._submit_login(page, _Inputs(username, password))

    assert button.click_count == 1
    assert button.scrolled is True
    assert checkbox.checked is True
    assert username.keys == ["Control+A", "Backspace", "Tab"]
    assert password.keys == ["Control+A", "Backspace", "Tab"]
    assert username.clicks == 1
    assert password.clicks == 1
    assert username.sequential == [("user-sentinel", 35)]
    assert password.sequential == [("pass-sentinel", 35)]
    assert username.fills == []
    assert password.fills == []
    assert username.events == []
    assert password.events == []
    assert page.waits == [100, 100]
    assert "user-sentinel" not in repr(trace)
    assert "pass-sentinel" not in repr(trace)
    assert [item["role"] for item in trace] == [
        "autofill_replayed",
        "privacy_checkbox",
        "login_button",
    ]


def test_yto_login_fills_dpapi_credentials_without_exposing_values(monkeypatch):
    checkbox = _Checkbox(False)
    button = _Button(checkbox)
    username = _Password("")
    password = _Password("")
    page = _Page(button, checkbox)
    writer = _writer()
    writer._scopes = lambda _page: [page]
    writer._wait_for_login_success = lambda _page, _seconds: True

    trace = writer._submit_login(
        page,
        _Inputs(username, password),
        YtoCredentials("account-sentinel", "password-sentinel"),
    )

    assert username.sequential == [("account-sentinel", 35)]
    assert password.sequential == [("password-sentinel", 35)]
    assert "account-sentinel" not in repr(trace)
    assert "password-sentinel" not in repr(trace)
    assert trace[0] == {"role": "dpapi_credentials_filled", "value": True}


def test_yto_login_page_is_refreshed_once_before_form_processing():
    page = _RefreshPage()

    YtoServiceChatWriter._refresh_login_page(page)

    assert page.reloads == [{"wait_until": "domcontentloaded"}]
    assert page.waits == [700]


def test_yto_open_window_recovers_login_before_returning(monkeypatch):
    writer = _writer()

    class Page:
        url = "https://kh.yto.net.cn/new/login"

        def set_default_timeout(self, _value):
            pass

        def bring_to_front(self):
            pass

    page = Page()

    class Context:
        pages = [page]

    class Browser:
        contexts = [Context()]

    class Playwright:
        def stop(self):
            pass

    monkeypatch.setattr(writer, "_connect", lambda: (Playwright(), Browser()))
    monkeypatch.setattr(writer, "_find_page", lambda _browser: page)

    def recover(_page):
        page.url = "https://kh.yto.net.cn/new/home"
        return [
            {"role": "dpapi_credentials_filled", "value": True},
            {"role": "login_button", "value": True},
        ]

    monkeypatch.setattr(writer, "_ensure_login", recover)

    result = writer.open_window()

    assert result["login_required"] is False
    assert result["login_recovered"] is True
    assert result["url"].endswith("/new/home")


def test_yto_login_uses_enter_only_once_when_click_does_not_submit(monkeypatch):
    checkbox = _Checkbox(True)
    button = _Button(checkbox)
    username = _Password("user-sentinel")
    password = _Password("pass-sentinel")
    page = _Page(button, checkbox)
    writer = _writer()
    writer._scopes = lambda _page: [page]
    outcomes = iter([False, True])
    writer._wait_for_login_success = lambda _page, _seconds: next(outcomes)

    trace = writer._submit_login(page, _Inputs(username, password))

    assert button.click_count == 1
    assert username.keys == ["Control+A", "Backspace", "Tab"]
    assert password.keys == ["Control+A", "Backspace", "Tab", "Enter"]
    assert [item["role"] for item in trace] == [
        "autofill_replayed",
        "login_button",
        "login_enter_fallback",
    ]


def test_yto_delivery_echo_count_excludes_compose_text():
    scope = _TextScope(
        [
            _TextItem({"x": 300, "y": 550, "width": 200, "height": 30}),
            _TextItem({"x": 300, "y": 630, "width": 200, "height": 30}),
        ]
    )

    count = _writer()._visible_delivery_echo_count(
        scope, "YT123456789+拦截", _EditorBox()
    )

    assert count == 1
