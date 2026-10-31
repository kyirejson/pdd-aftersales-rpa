from pathlib import Path
from uuid import uuid4

import pytest

from pdd_rpa.components import ComponentValidationError, validate_component, validate_workflow
from pdd_rpa.db import Repository


def test_click_component_requires_locator():
    with pytest.raises(ComponentValidationError):
        validate_component({"component_type": "ClickElement", "name": "立即处理"})


def test_unknown_component_type_is_rejected():
    with pytest.raises(ComponentValidationError):
        validate_component({"component_type": "Unknown", "name": "未知"})


def test_workflow_rejects_duplicate_node_ids():
    with pytest.raises(ComponentValidationError):
        validate_workflow({"name": "重复", "nodes": [{"node_id": "a"}, {"node_id": "a"}]})


def test_repository_round_trip_components_and_workflows():
    database = Path("runtime") / f"test-components-{uuid4().hex}.db"
    repo = Repository(database)
    repo.initialize()
    component = validate_component({
        "component_type": "ClickElement",
        "name": "立即处理",
        "locator": {"text": "立即处理"},
    })
    repo.save_component(component)
    assert repo.get_component(component["component_id"])["name"] == "立即处理"

    workflow = validate_workflow({
        "name": "详情采集",
        "nodes": [{"node_id": "n1", "component_id": component["component_id"]}],
    })
    repo.save_workflow(workflow)
    assert repo.list_workflows()[0]["name"] == "详情采集"
    database.unlink(missing_ok=True)
