"""BitableClient 附件接口 —— 附件单元格归一化 + 媒体下载 + V3 两个附件列。

用桩 lark client(不触网)驱动真实 BitableClient:
- attachment_items:把附件列原始值(list[{file_token,name,size}])归一化,
  空/缺列/异常形态返回空列表;V3 支持指定列(导入用 / 过程记录),
  并读出声明的 size(V3-P10 体积预检的唯一依据);
- submitter_text / submit_time_text:人员列 / 日期列 -> 展示文本;
- download_attachment:调 drive.v1.media.download,二进制响应返回 bytes;
  失败码 -> BitableError、限流码/网络异常 -> RetryableError。
"""
import io
from types import SimpleNamespace

import pytest

from helpers import attachment_cell

from preset_sync_worker import fields
from preset_sync_worker.adapters.bitable import (
    AttachmentItem,
    BitableClient,
    date_text,
    person_names,
)
from preset_sync_worker.exceptions import BitableError, RetryableError


class FakeDownloadResponse:
    """模拟 DownloadMediaResponse:成功时 file 为 BytesIO,失败带 code/msg。"""

    def __init__(self, *, success=True, code=0, msg="success", file=None):
        self._success = success
        self.code = code
        self.msg = msg
        self.file = file

    def success(self):
        return self._success


def make_client(payload=b"{}", error=None, code=0, msg="ok"):
    """drive.v1.media.download 的桩;返回 (client, 收到的请求列表)。"""
    calls: list = []

    def _download(request):
        calls.append(request)
        if error is not None:
            raise error
        if code != 0:
            return FakeDownloadResponse(
                success=False, code=code, msg=msg, file=None
            )
        return FakeDownloadResponse(file=io.BytesIO(payload))

    client = SimpleNamespace(
        drive=SimpleNamespace(v1=SimpleNamespace(media=SimpleNamespace(download=_download)))
    )
    return client, calls


def make_bitable(client):
    return BitableClient(client=client, app_token="app-t", table_id="tbl-t")


# ----------------------------------------------------------------------
# 附件单元格归一化
# ----------------------------------------------------------------------
def test_attachment_items_normalizes_cell():
    """附件列原始值(list[{file_token,name,size}])归一化为 AttachmentItem。"""
    client, _ = make_client()
    bitable = make_bitable(client)

    items = bitable.attachment_items(
        {
            fields.PROFILE_JSON: [
                {"file_token": "tok-1", "name": "profile.json", "size": 42},
                {"file_token": "tok-2", "name": "photo.png"},
            ]
        }
    )

    assert items == [
        AttachmentItem(file_token="tok-1", name="profile.json", size=42),
        AttachmentItem(file_token="tok-2", name="photo.png", size=None),
    ]


def test_attachment_items_ignores_non_int_size():
    """size 形态异常(字符串/浮点)一律当缺失 —— 体积预检不能因此误判。"""
    client, _ = make_client()
    bitable = make_bitable(client)

    items = bitable.attachment_items(
        {
            fields.PROFILE_JSON: [
                {"file_token": "tok-1", "name": "a.json", "size": "42"},
                {"file_token": "tok-2", "name": "b.json", "size": 4.2},
                {"file_token": "tok-3", "name": "c.json", "size": True},
            ]
        }
    )

    assert [i.size for i in items] == [None, None, None]


def test_attachment_items_reads_requested_column():
    """V3:两个附件列各自独立 —— 默认 Profile JSON,可指定 过程记录。"""
    client, _ = make_client()
    bitable = make_bitable(client)
    row = {
        fields.PROFILE_JSON: attachment_cell("profile.json"),
        fields.PROCESS_RECORD: attachment_cell("log.txt", "todo.md"),
    }

    assert [i.name for i in bitable.attachment_items(row)] == ["profile.json"]
    assert [
        i.name
        for i in bitable.attachment_items(row, fields.PROCESS_RECORD)
    ] == ["log.txt", "todo.md"]


def test_attachment_items_returns_empty_for_absent_or_malformed_cell():
    client, _ = make_client()
    bitable = make_bitable(client)

    assert bitable.attachment_items({}) == []
    assert bitable.attachment_items({fields.PROFILE_JSON: None}) == []
    assert bitable.attachment_items({fields.PROFILE_JSON: "not-a-list"}) == []
    # 畸形条目(缺 token/name)静默忽略
    assert bitable.attachment_items(
        {fields.PROFILE_JSON: [{"file_token": "t"}, {"name": "x"}, "junk"]}
    ) == []


# ----------------------------------------------------------------------
# 下载
# ----------------------------------------------------------------------
def test_download_attachment_returns_bytes():
    payload = b'{"name": "Test PLA"}'
    client, calls = make_client(payload=payload)
    bitable = make_bitable(client)

    content = bitable.download_attachment("tok-1")

    assert content == payload
    assert calls[0].file_token == "tok-1"


def test_download_attachment_failure_code_raises_bitable_error():
    client, _ = make_client(code=12345, msg="media not found")
    bitable = make_bitable(client)

    with pytest.raises(BitableError) as exc:
        bitable.download_attachment("tok-1")
    assert "media not found" in str(exc.value)


def test_download_attachment_rate_limited_raises_retryable():
    client, _ = make_client(code=99991400, msg="访问频繁")
    bitable = make_bitable(client)

    with pytest.raises(RetryableError):
        bitable.download_attachment("tok-1")


def test_download_attachment_transport_error_raises_retryable():
    client, _ = make_client(error=TimeoutError("timeout"))
    bitable = make_bitable(client)

    with pytest.raises(RetryableError):
        bitable.download_attachment("tok-1")


def test_download_attachment_missing_body_raises_bitable_error():
    """成功响应但没有文件体:按数据问题报错,不返回空字节假装成功。"""
    client, _ = make_client()
    bitable = make_bitable(client)
    bitable.client.drive.v1.media.download = lambda req: FakeDownloadResponse(
        file=None
    )

    with pytest.raises(BitableError):
        bitable.download_attachment("tok-1")


# ----------------------------------------------------------------------
# V3-P7:提交人 / 提交时间(只读展示列)
# ----------------------------------------------------------------------
def test_person_names_handles_shapes():
    assert person_names([{"id": "u1", "name": "张三"}]) == ["张三"]
    assert person_names([{"id": "u1", "en_name": "San Zhang"}]) == ["San Zhang"]
    assert person_names([{"name": " 张三 "}, {"name": "李四"}]) == ["张三", "李四"]
    assert person_names("张三") == ["张三"]
    assert person_names([]) == []
    assert person_names(None) == []
    assert person_names([{"id": "u1"}]) == []


def test_date_text_parses_millis():
    # 2026-09-11 10:23 UTC+8 -> 毫秒时间戳
    assert date_text(1770000000000)  # 形态正确即可(本地时区无关)
    assert date_text("") == ""
    assert date_text(None) == ""
    assert date_text("2026-09-11 10:23") == "2026-09-11 10:23"  # 已是文本


def test_submitter_text_falls_back_to_placeholder():
    assert BitableClient.submitter_text(
        {fields.SUBMITTER: [{"id": "u1", "name": "张三"}]}
    ) == "张三"
    assert BitableClient.submitter_text(
        {fields.SUBMITTER: [{"name": "张三"}, {"name": "李四"}]}
    ) == "张三、李四"
    assert BitableClient.submitter_text({}) == "(未记录)"


def test_submit_time_text_falls_back_to_placeholder():
    assert BitableClient.submit_time_text({}) == "(未记录)"
    assert BitableClient.submit_time_text({fields.SUBMIT_TIME: ""}) == "(未记录)"
    text = BitableClient.submit_time_text({fields.SUBMIT_TIME: 1770000000000})
    assert text != "(未记录)"
