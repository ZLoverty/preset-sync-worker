"""MaterialWorker:单条记录异常不中断本轮;daemon 级异常不终止(P6 #30)。"""
from material_worker.worker import MaterialWorker


class StubBitable:
    def __init__(self, records):
        self.records = list(records)
        self.schema_checked = False

    def ensure_schema(self):
        self.schema_checked = True

    def list_pending_records(self):
        # 与真实 BitableClient.list_pending_records 一致:只返回 已请求=true
        return [(rid, f) for rid, f in self.records if f.get("已请求") is True]


class CountingService:
    def __init__(self, boom_on=None):
        self.calls: list[str] = []
        self.boom_on = boom_on
        self.sync_calls = 0

    def process_record(self, record_id, fields_map):
        self.calls.append(record_id)
        if self.boom_on == record_id:
            raise RuntimeError("模拟单条记录异常")

    def sync_reviewing_rows(self):
        self.sync_calls += 1


def test_boot_runs_ensure_schema():
    bitable = StubBitable([])
    worker = MaterialWorker(bitable=bitable, submission_service=CountingService())

    worker.boot()

    assert bitable.schema_checked


def test_poll_once_isolates_record_errors():
    bitable = StubBitable([("rec-1", {"已请求": True}), ("rec-2", {"已请求": True})])
    service = CountingService(boom_on="rec-1")
    worker = MaterialWorker(bitable=bitable, submission_service=service)

    worker.poll_once()  # rec-1 抛错不应中断 rec-2

    assert service.calls == ["rec-1", "rec-2"]


def test_poll_once_only_hands_out_pending_records():
    bitable = StubBitable([("rec-1", {"已请求": True}), ("rec-2", {"已请求": False})])
    service = CountingService()
    worker = MaterialWorker(bitable=bitable, submission_service=service)

    worker.poll_once()

    assert service.calls == ["rec-1"]


def test_poll_once_runs_review_sync():
    """每轮轮询除待处理行外,还会推进一次审查同步(审核中 行的 PR 状态)。"""
    bitable = StubBitable([])
    service = CountingService()
    worker = MaterialWorker(bitable=bitable, submission_service=service)

    worker.poll_once()
    worker.poll_once()

    assert service.sync_calls == 2
