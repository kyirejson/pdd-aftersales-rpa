from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC = PROJECT_ROOT / "pdd_rpa" / "static"


def test_simple_route_editor_exposes_toggle_and_preserves_active_edit_form():
    script = (STATIC / "routing.js").read_text(encoding="utf-8")

    assert "async function toggleRoute(index)" in script
    assert 'toggle.textContent = route.enabled === false ? "启用" : "停用"' in script
    assert 'toggle.addEventListener("click", () => toggleRoute(index))' in script
    assert "route.enabled = nextEnabled" in script
    assert "{ resetForm: false }" in script


def test_deleting_last_route_prunes_only_its_unused_warehouse_whitelist_entry():
    script = (STATIC / "routing.js").read_text(encoding="utf-8")

    assert "const hasAnotherWarehouseRoute" in script
    assert "itemIndex !== index && routeWarehouse(item) === warehouse" in script
    assert "if (!hasAnotherWarehouseRoute)" in script
    assert ".filter((item) => item && item !== warehouse)" in script
    assert "最后一条货品路由" in script


def test_new_warehouse_requires_confirmation_of_normalized_name():
    script = (STATIC / "routing.js").read_text(encoding="utf-8")

    assert "const isNewWarehouse = !knownWarehouses().includes(values.warehouse)" in script
    assert "将新增在途仓库白名单" in script
    assert "请确认仓库名称和文字完全正确" in script
    assert "已取消保存；仓库白名单没有变更" in script


def test_routing_page_cachebuster_and_wechat_designer_match_current_roles():
    page = (STATIC / "routing.html").read_text(encoding="utf-8")
    designer = (STATIC / "designer.js").read_text(encoding="utf-8")

    assert "/static/routing.js?v=20260828-1" in page
    assert "/static/routing.css?v=20260828-1" in page
    wechat_block = designer.split("wechat: [", 1)[1].split("],\n};", 1)[0]
    assert "CMP-MSG-WECHAT-SEARCH-INPUT" in wechat_block
    assert "CMP-MSG-WECHAT-GROUP-HEADER" in wechat_block
    assert "CMP-MSG-WECHAT-SEARCH-RESULT" not in wechat_block
