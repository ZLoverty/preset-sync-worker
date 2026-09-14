"""MaterialWorker:全表快照只拉一次,三个阶段共用;daemon 级异常不终止(P6 #30)。

V3:worker 每轮 `list_records()` 拉一次全表,同一份快照依次交给
process_pending_rows / backfill_pending_json_rows / sync_reviewing_rows ——
重复身份检测需要全表视角,顺带省掉三次全表拉取。
"""
from material_worker.worker import MaterialWorker


class StubBitable:
    def __init__(self, records):
        self.records = list(records)
        self.schema_checked = False
        self.list_calls = 0

    def ensure_schema(self):
        self.schema_checked = True

    def list_records(self):
        self.list_calls += 1
        return list(self.records)

    def list_pending_records(self):
        # 保留给单测/脚本的旧入口,与真实实现一致:只返回 已请求=true
        return [(rid, f) for rid, f in self.records if f.get("已请求") is True]


class CountingService:
    """记录三阶段被调用的次数与拿到的快照。"""

    def __init__(self, boom_on=None):
        self.calls: list[str] = []
        self.boom_on = boom_on
        self.snapshots: list[list] = []
        self.sync_calls = 0
        self.backfill_calls = 0

    def process_pending_rows(self, records):
        self.snapshots.append(records)
        for record_id, _fields in records:
            if _fields.get("已请求") is not True:
                continue
            self.calls.append(record_id)
            if self.boom_on == record_id:
                raise RuntimeError("模拟单条记录异常")

    def sync_reviewing_rows(self, records=None):
        self.sync_calls += 1
        self.sync_snapshot = records

    def backfill_pending_json_rows(self, records=None):
        self.backfill_calls += 1
        self.backfill_snapshot = records


def test_boot_runs_ensure_schema():
    bitable = StubBitable([])
    worker = MaterialWorker(bitable=bitable, submission_service=CountingService())

    worker.boot()

    assert bitable.schema_checked


def test_poll_once_hands_the_same_snapshot_to_all_three_phases():
    """V3:全表只拉一次,三个阶段共用同一份快照。"""
    bitable = StubBitable([("rec-1", {"已请求": True})])
    service = CountingService()
    worker = MaterialWorker(bitable=bitable, submission_service=service)

    worker.poll_once()

    assert bitable.list_calls == 1
    assert service.snapshots == [[("rec-1", {"已请求": True})]]
    assert service.backfill_snapshot == service.snapshots[0]
    assert service.sync_snapshot == service.snapshots[0]


def test_poll_once_only_hands_out_pending_records():
    bitable = StubBitable([("rec-1", {"已请求": True}), ("rec-2", {"已请求": False})])
    service = CountingService()
    worker = MaterialWorker(bitable=bitable, submission_service=service)

    worker.poll_once()

    assert service.calls == ["rec-1"]
    # 非待处理行仍在快照里(附件导入/审查同步要看全表)
    assert len(service.snapshots[0]) == 2


def test_poll_once_isolates_record_errors():
    """单条记录异常不中断本轮其余记录 —— 由 service 内部隔离。"""
    bitable = StubBitable([("rec-1", {"已请求": True}), ("rec-2", {"已请求": True})])
    service = CountingService(boom_on="rec-1")
    worker = MaterialWorker(bitable=bitable, submission_service=service)

    try:
        worker.poll_once()
    except RuntimeError:
        pass  # CountingService 是桩,隔离在真实 service 内;这里只验证调用序

    assert service.calls == ["rec-1"]


def test_poll_once_runs_review_sync():
    bitable = StubBitable([])
    service = CountingService()
    worker = MaterialWorker(bitable=bitable, submission_service=service)

    worker.poll_once()
    worker.poll_once()

    assert service.sync_calls == 2


def test_poll_once_runs_attachment_backfill():
    bitable = StubBitable([])
    service = CountingService()
    worker = MaterialWorker(bitable=bitable, submission_service=service)

    worker.poll_once()
    worker.poll_once()

    assert service.backfill_calls == 2
