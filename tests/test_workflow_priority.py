from copy import deepcopy

from pdd_rpa.workflow_priority import (
    DEFAULT_CONFIG,
    CONFIRM_EARLIER_SHIPPING,
    IN_TRANSIT_NO_REASON,
    SHIPPED_NO_TRACE,
    prioritize_tasks,
    validate_config,
)


def task(task_id, title, deadline="", created="2026-08-05T10:00:00"):
    return {
        "id": task_id,
        "created_at": created,
        "facts": {"work_order_title": title, "work_order_deadline": deadline},
    }


def test_shipped_no_trace_is_default_highest_priority():
    tasks = [
        task(1, IN_TRANSIT_NO_REASON, "2026-08-05T11:00:00"),
        task(2, SHIPPED_NO_TRACE, "2026-08-05T12:00:00"),
        task(3, CONFIRM_EARLIER_SHIPPING, "2026-08-05T10:00:00"),
    ]
    result = prioritize_tasks(tasks, DEFAULT_CONFIG)
    assert [item["id"] for item in result] == [2, 1, 3]
    assert result[0]["queue_priority"] == 100
    assert result[1]["queue_priority"] == 50
    assert result[2]["queue_priority"] == 40


def test_priority_is_configurable_and_deadline_breaks_ties():
    config = deepcopy(DEFAULT_CONFIG)
    for item in config["workflows"]:
        item["priority"] = 80
    tasks = [
        task(1, IN_TRANSIT_NO_REASON, "2026-08-05T12:00:00"),
        task(2, SHIPPED_NO_TRACE, "2026-08-05T11:00:00"),
    ]
    assert [item["id"] for item in prioritize_tasks(tasks, config)] == [2, 1]


def test_page_scan_order_precedes_deadline_within_same_workflow():
    config = deepcopy(DEFAULT_CONFIG)
    tasks = [
        {
            **task(1, SHIPPED_NO_TRACE, "2026-08-05T10:00:00"),
            "facts": {
                **task(1, SHIPPED_NO_TRACE)["facts"],
                "source_evidence": {"scan_order": 10002, "page_no": 2, "page_index": 0},
            },
        },
        {
            **task(2, SHIPPED_NO_TRACE, "2026-08-05T11:00:00"),
            "facts": {
                **task(2, SHIPPED_NO_TRACE)["facts"],
                "source_evidence": {"scan_order": 10001, "page_no": 1, "page_index": 0},
            },
        },
    ]
    assert [item["id"] for item in prioritize_tasks(tasks, config)] == [2, 1]


def test_priority_validation_migrates_missing_new_aftersale_workflow():
    config = deepcopy(DEFAULT_CONFIG)
    config["workflows"].pop()
    result = validate_config(config)
    assert [item["workflow_key"] for item in result["workflows"]][-1] == "aftersale_shipped_refund"


def test_priority_validation_migrates_legacy_two_workflows():
    config = deepcopy(DEFAULT_CONFIG)
    config["workflows"] = config["workflows"][:2]
    result = validate_config(config)
    assert [item["workflow_key"] for item in result["workflows"]] == [
        "shipped_no_trace",
        "in_transit_no_reason",
        "confirm_earlier_shipping",
        "aftersale_return_refund",
        "aftersale_shipped_refund",
    ]


def test_priority_validation_migrates_incomplete_config():
    config = deepcopy(DEFAULT_CONFIG)
    config["workflows"] = config["workflows"][:1]
    result = validate_config(config)
    assert {item["workflow_key"] for item in result["workflows"]} == {
        "shipped_no_trace",
        "in_transit_no_reason",
        "confirm_earlier_shipping",
        "aftersale_return_refund",
        "aftersale_shipped_refund",
    }


def test_priority_validation_rejects_duplicate_aftersale_shipped_workflow():
    config = deepcopy(DEFAULT_CONFIG)
    config["workflows"].append(
        {
            "workflow_key": "aftersale_shipped_refund",
            "title": "售后工作台：已发货-退款",
            "priority": 20,
            "enabled": True,
        }
    )
    import pytest

    with pytest.raises(Exception, match="重复"):
        validate_config(config)
