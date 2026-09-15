import logging

from preset_sync_worker.logging_setup import log


def test_retry_and_user_action_logs_have_distinct_levels(caplog):
    with caplog.at_level(logging.INFO):
        log("[重试] record=rec-test (1/5): Git API 网络错误")
        log("[数据/永久失败] record=rec-test: 缺少必填字段: 喷嘴温度")

    assert [record.levelno for record in caplog.records] == [
        logging.WARNING,
        logging.ERROR,
    ]
    assert "record=rec-test" in caplog.text
    assert "缺少必填字段" in caplog.text
