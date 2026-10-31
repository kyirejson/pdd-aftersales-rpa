import pytest

from pdd_rpa.qianniu_wdt import (
    QianniuWdtError,
    validate_qianniu_lookup_identifiers,
    validate_qianniu_wdt_context,
)


def valid_context(**overrides):
    result = {
        "wdt_result_count": 1,
        "wdt_waybill": "40000000000000",
        "wdt_order_id": "WDT-001",
        "wdt_platform": "淘宝",
        "wdt_original_order": "5127711625084043334",
        "wdt_order_status": "已发货",
        "wdt_warehouse": "示例自营仓",
        "wdt_logistics_company": "韵达",
        "wdt_product_names": "",
    }
    result.update(overrides)
    return result


def test_qianniu_wdt_accepts_taobao_exact_identity():
    result = validate_qianniu_wdt_context(
        valid_context(),
        order_no="5127711625084043334",
        tracking_no="40000000000000",
    )

    assert result["ok"] is True
    assert result["platform"] == "淘宝"
    assert result["order_status"] == "已发货"
    assert result["business_write_performed"] is False


def test_qianniu_wdt_keeps_yto_alpha_prefix_for_lookup():
    assert validate_qianniu_lookup_identifiers(
        "3316373043044030886", "YT7968643252039"
    ) == ("3316373043044030886", "YT7968643252039")


def test_qianniu_wdt_rejects_pdd_platform():
    with pytest.raises(QianniuWdtError):
        validate_qianniu_wdt_context(
            valid_context(wdt_platform="拼多多"),
            order_no="5127711625084043334",
            tracking_no="40000000000000",
        )


def test_qianniu_wdt_records_but_allows_different_original_order():
    result = validate_qianniu_wdt_context(
        valid_context(wdt_original_order="5127000000000000000"),
        order_no="5127711625084043334",
        tracking_no="40000000000000",
    )

    assert result["original_order_matches"] is False
    assert result["wdt_original_order"] == "5127000000000000000"


def test_qianniu_wdt_recovers_warehouse_and_logistics_from_painted_full_row():
    row = (
        "5127709501019017748\t已完成\t客户\t湖北省荆州市\t款到发货"
        "\t示例自营仓\t韵达一淘1"
    )
    result = validate_qianniu_wdt_context(
        valid_context(wdt_warehouse=row, wdt_logistics_company=row),
        order_no="5127711625084043334",
        tracking_no="40000000000000",
    )

    assert result["warehouse"] == "示例自营仓"
    assert result["logistics_company"] == "韵达一淘1"
    assert result["grid_row_recovered"] is True
    assert result["warehouse_raw"] == row


def test_qianniu_wdt_allows_visual_zero_only_with_exact_row_evidence():
    result = validate_qianniu_wdt_context(
        valid_context(wdt_result_count=0),
        order_no="5127711625084043334",
        tracking_no="40000000000000",
    )

    assert result["unique_result_verified_by"] == "exact_tracking_platform_unique_row"
