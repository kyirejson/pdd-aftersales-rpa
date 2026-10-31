from types import SimpleNamespace

from pdd_rpa.edge_uia import PddEdgeUiaSession


class _RawRoot:
    def descendants(self):
        return [
            SimpleNamespace(control_type=None, name="transient"),
            SimpleNamespace(control_type="Text", name="page"),
            SimpleNamespace(control_type="Edit", name="地址和搜索栏"),
        ]


class _Backend:
    @staticmethod
    def generic_wrapper_class(element_info):
        if element_info.control_type is None:
            raise KeyError(None)
        return SimpleNamespace(
            element_info=element_info,
            get_value=lambda: "https://mms.pinduoduo.com/aftersales/work_order/list",
        )


class _UiaWindow:
    element_info = _RawRoot()
    backend = _Backend()


def test_current_url_skips_transient_untyped_uia_descendant():
    session = object.__new__(PddEdgeUiaSession)
    session.Desktop = lambda backend: SimpleNamespace(
        window=lambda handle: _UiaWindow()
    )
    window = SimpleNamespace(handle=42)

    assert session._current_url(window).endswith("/aftersales/work_order/list")
