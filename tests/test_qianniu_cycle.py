import pytest

from pdd_rpa.qianniu_cycle import (
    QianniuCycleError,
    build_cycle_contract,
)


def config(**overrides):
    value = {
        "workflow_enabled": False,
        "production_enabled": False,
        "scan_position": 12,
        "scan_after_pdd_shop_count": 11,
        "process_after_all_pdd_tasks": True,
    }
    value.update(overrides)
    return value


def test_qianniu_is_scanned_twelfth_and_processed_after_all_pdd_tasks():
    result = build_cycle_contract(
        [f"shop_{index:02d}" for index in range(1, 12)],
        "qianniu_01",
        config(),
    )

    assert [item["position"] for item in result["scan_order"]] == list(
        range(1, 13)
    )
    assert result["scan_order"][-1] == {
        "position": 12,
        "platform": "qianniu",
        "shop_id": "qianniu_01",
    }
    assert result["processing_phases"][0]["platform"] == "pdd"
    assert result["processing_phases"][1]["platform"] == "qianniu"
    assert result["processing_phases"][1]["requires_pdd_phase_complete"] is True


def test_qianniu_contract_rejects_any_other_pdd_shop_count():
    with pytest.raises(QianniuCycleError):
        build_cycle_contract(
            [f"shop_{index:02d}" for index in range(1, 11)],
            "qianniu_01",
            config(),
        )


def test_production_cannot_be_enabled_while_workflow_is_disabled():
    with pytest.raises(QianniuCycleError):
        build_cycle_contract(
            [f"shop_{index:02d}" for index in range(1, 12)],
            "qianniu_01",
            config(production_enabled=True),
        )
