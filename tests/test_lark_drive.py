"""DriveClient —— 飞书云空间上传(列目录 / 建目录 / 上传)。

用桩 lark client(不触网)驱动真实 DriveClient:
- publish:逐层先查后建,目录内按名跳过,重试幂等;
- 落盘名规划:清洗非法字符、批内重名加后缀,且必须是确定性的;
- 体积上限:超限在建目录之前就抛 PermanentError(一个请求都不发);
- 错误映射:传输异常/限流码 -> RetryableError,其余错误码 -> DriveError。
"""
import io
from types import SimpleNamespace

import pytest

from material_worker.adapters.lark_drive import (
    DriveClient,
    DriveUpload,
    plan_names,
)
from material_worker.exceptions import DriveError, PermanentError, RetryableError

ROOT = "root-token"


# ----------------------------------------------------------------------
# 桩:一个能当文件系统用的假云空间
# ----------------------------------------------------------------------
class FakeFile:
    def __init__(self, token, name, type, url=""):
        self.token = token
        self.name = name
        self.type = type
        self.url = url


class FakeResponse:
    def __init__(self, *, success=True, code=0, msg="ok", data=None):
        self._success = success
        self.code = code
        self.msg = msg
        self.data = data

    def success(self):
        return self._success


class FakeDrive:
    """记录每个请求,并维护一棵足以支撑幂等断言的目录树。"""

    def __init__(self, *, pages=None, error=None, code=0, msg="ok"):
        self.nodes: dict[str, list[FakeFile]] = {}  # folder_token -> children
        self.tokens = 0
        self.requests: list[str] = []  # 只记操作名,供元测试断言
        self.uploaded: list[tuple[str, str, bytes]] = []  # (parent, name, bytes)
        self.list_calls: list[tuple[str, str | None]] = []  # (folder, page_token)
        self.page_sizes: list[int] = []
        self._pages = pages  # {folder_token: [[第1页], [第2页], ...]}
        self._error = error  # 抛出的传输层异常
        self._code = code  # 非 0 -> 失败响应
        self._msg = msg

    # -- 内部 ---------------------------------------------------------
    def _next_token(self, prefix="boxcn"):
        self.tokens += 1
        return f"{prefix}{self.tokens:04d}"

    def _fail(self):
        if self._error is not None:
            raise self._error
        if self._code:
            return FakeResponse(success=False, code=self._code, msg=self._msg)
        return None

    def _page_of(self, folder_token):
        if self._pages is not None:
            return self._pages.get(folder_token, [[]])
        return [self.nodes.setdefault(folder_token, [])]

    # -- SDK 面 -------------------------------------------------------
    def list(self, request):
        self.requests.append("list")
        self.list_calls.append((request.folder_token, request.page_token))
        self.page_sizes.append(request.page_size)
        failed = self._fail()
        if failed is not None:
            return failed

        pages = self._page_of(request.folder_token)
        index = int(request.page_token or 0)
        page = pages[index] if index < len(pages) else []
        has_more = index + 1 < len(pages)
        return FakeResponse(
            data=SimpleNamespace(
                files=page,
                has_more=has_more,
                next_page_token=str(index + 1) if has_more else None,
            )
        )

    def create_folder(self, request):
        self.requests.append("create_folder")
        failed = self._fail()
        if failed is not None:
            return failed
        body = request.request_body
        node = FakeFile(self._next_token("fld"), body.name, "folder")
        self.nodes.setdefault(body.folder_token, []).append(node)
        return FakeResponse(
            data=SimpleNamespace(
                token=node.token, url=f"https://x/drive/folder/{node.token}"
            )
        )

    def upload_all(self, request):
        self.requests.append("upload_all")
        failed = self._fail()
        if failed is not None:
            return failed
        body = request.request_body
        content = body.file.read()
        self.uploaded.append((body.parent_node, body.file_name, content))
        node = FakeFile(self._next_token("boxcn"), body.file_name, "file")
        self.nodes.setdefault(body.parent_node, []).append(node)
        return FakeResponse(data=SimpleNamespace(file_token=node.token))


def make_client(drive=None, **kwargs):
    drive = drive or FakeDrive()
    client = SimpleNamespace(
        drive=SimpleNamespace(v1=SimpleNamespace(file=drive))
    )
    return DriveClient(client, ROOT, **kwargs), drive


def upload(name, content=b"x"):
    return DriveUpload(name=name, content=content)


# ----------------------------------------------------------------------
# publish:目录
# ----------------------------------------------------------------------
def test_publish_creates_each_level_then_uploads():
    client, drive = make_client()

    result = client.publish(("2026-09", "20260912-142530-817_rec1"), [upload("a.jpg")])

    # 两层目录各建一次,文件落在叶子层
    assert [r for r in drive.requests] == ["list", "create_folder", "list", "create_folder", "list", "upload_all"]
    assert drive.uploaded[0][0] == result.folder_token
    assert drive.uploaded[0][1] == "a.jpg"


def test_publish_reuses_existing_folder_and_never_recreates():
    client, drive = make_client()
    leaf = FakeFile("fld-existing", "20260912-142530-817_rec1", "folder")
    drive.nodes[ROOT] = [FakeFile("fld-month", "2026-09", "folder")]
    drive.nodes["fld-month"] = [leaf]

    result = client.publish(("2026-09", "20260912-142530-817_rec1"), [upload("a.jpg")])

    assert result.folder_token == "fld-existing"
    assert "create_folder" not in drive.requests


def test_publish_ignores_same_named_file_when_looking_for_folder():
    """同名**文件**不能顶替文件夹,否则会把附件传进一个不存在的地方。"""
    client, drive = make_client()
    drive.nodes[ROOT] = [FakeFile("boxcn-file", "2026-09", "file")]

    client.publish(("2026-09",), [upload("a.jpg")])

    assert drive.requests.count("create_folder") == 1
    assert drive.uploaded[0][0] == "fld0001"  # 新建出来的那个目录,不是同名文件


def test_publish_upload_request_carries_expected_fields():
    client, drive = make_client()

    client.publish(("2026-09",), [upload("a.jpg", b"hello")])

    assert drive.uploaded == [(drive.uploaded[0][0], "a.jpg", b"hello")]


def test_publish_wraps_content_in_io_base_not_raw_bytes():
    """钉死 SDK 的坑:parse_form_data 只放行 IOBase,裸 bytes 会被 str() 成 b'...'。"""
    captured = {}

    class RecordingDrive(FakeDrive):
        def upload_all(self, request):
            stream = request.request_body.file
            captured["file"] = stream
            captured["size"] = request.request_body.size
            # 先读出来再交还给父类(父类也要 read 一次)
            captured["content"] = stream.read()
            stream.seek(0)
            return super().upload_all(request)

    client, _ = make_client(RecordingDrive())
    client.publish(("2026-09",), [upload("a.jpg", b"hello")])

    assert isinstance(captured["file"], io.IOBase)
    assert not isinstance(captured["file"], (bytes, bytearray))
    assert captured["content"] == b"hello"
    assert captured["size"] == 5


def test_publish_skips_files_already_in_folder():
    """重试时的正常态:已存在的不重传,但仍计入结果。"""
    client, drive = make_client()
    leaf = FakeFile("fld-leaf", "2026-09", "folder")
    drive.nodes[ROOT] = [leaf]
    drive.nodes["fld-leaf"] = [FakeFile("boxcn-old", "a.jpg", "file")]

    result = client.publish(("2026-09",), [upload("a.jpg"), upload("b.jpg")])

    assert result.uploaded == 1
    assert result.skipped == 1
    assert [u[1] for u in drive.uploaded] == ["b.jpg"]


def test_publish_is_idempotent_on_second_call():
    client, drive = make_client()
    uploads = [upload("a.jpg", b"1"), upload("b.jpg", b"2")]

    first = client.publish(("2026-09", "leaf"), uploads)
    second = client.publish(("2026-09", "leaf"), uploads)

    assert first.uploaded == 2 and first.skipped == 0
    assert second.uploaded == 0 and second.skipped == 2
    assert second.folder_token == first.folder_token
    assert first.folder_url == second.folder_url


def test_publish_disambiguates_repeated_names_deterministically():
    client, drive = make_client()
    uploads = [upload("log.txt", b"1"), upload("log.txt", b"2"), upload("log.txt", b"3")]

    client.publish(("2026-09",), uploads)

    assert [u[1] for u in drive.uploaded] == ["log.txt", "log (2).txt", "log (3).txt"]


def test_publish_repeated_names_idempotent_on_retry():
    """重名批次重试:算出的名字必须一样,否则会重复上传。"""
    client, drive = make_client()
    uploads = [upload("log.txt", b"1"), upload("log.txt", b"2")]

    client.publish(("2026-09",), uploads)
    second = client.publish(("2026-09",), uploads)

    assert second.uploaded == 0 and second.skipped == 2


def test_publish_empty_batch_still_creates_folder():
    client, drive = make_client()

    result = client.publish(("2026-09", "leaf"), [])

    assert drive.uploaded == []
    assert result.folder_token
    assert result.uploaded == 0 and result.skipped == 0


def test_publish_folder_url_uses_host():
    client, _ = make_client(host="example.feishu.cn")

    result = client.publish(("2026-09",), [upload("a.jpg")])

    assert result.folder_url == f"https://example.feishu.cn/drive/folder/{result.folder_token}"


# ----------------------------------------------------------------------
# publish:体积与名字的前置校验(必须在发请求之前)
# ----------------------------------------------------------------------
def test_publish_oversized_file_raises_permanent_without_any_request():
    client, drive = make_client(max_file_bytes=10)

    with pytest.raises(PermanentError) as exc:
        client.publish(("2026-09",), [upload("big.jpg", b"x" * 11)])

    assert "big.jpg" in str(exc.value)
    assert drive.requests == []


def test_publish_rejects_oversize_even_if_other_files_are_fine():
    """整批拒绝:绝不"传了一半才发现"。"""
    client, drive = make_client(max_file_bytes=10)

    with pytest.raises(PermanentError):
        client.publish(("2026-09",), [upload("ok.jpg", b"x"), upload("big.jpg", b"x" * 11)])

    assert drive.requests == []


def test_publish_blank_name_raises_permanent_without_any_request():
    client, drive = make_client()

    with pytest.raises(PermanentError):
        client.publish(("2026-09",), [upload("   ")])

    assert drive.requests == []


def test_publish_at_exactly_the_limit_is_allowed():
    client, drive = make_client(max_file_bytes=4)

    client.publish(("2026-09",), [upload("a.jpg", b"abcd")])

    assert len(drive.uploaded) == 1


# ----------------------------------------------------------------------
# 错误映射
# ----------------------------------------------------------------------
def test_transport_error_is_retryable():
    client, _ = make_client(FakeDrive(error=TimeoutError("timeout")))

    with pytest.raises(RetryableError):
        client.publish(("2026-09",), [upload("a.jpg")])


def test_rate_limit_code_is_retryable():
    client, _ = make_client(FakeDrive(code=99991400, msg="访问频繁"))

    with pytest.raises(RetryableError):
        client.publish(("2026-09",), [upload("a.jpg")])


def test_api_error_code_is_drive_error_with_code_and_msg():
    client, _ = make_client(FakeDrive(code=1061002, msg="no permission"))

    with pytest.raises(DriveError) as exc:
        client.publish(("2026-09",), [upload("a.jpg")])

    assert "1061002" in str(exc.value) and "no permission" in str(exc.value)


def test_create_folder_error_is_mapped_too():
    client, _ = make_client(FakeDrive(code=1061002, msg="no permission"))

    with pytest.raises(DriveError):
        client.publish(("2026-09",), [])


# ----------------------------------------------------------------------
# 分页(check_access / 列目录)
# ----------------------------------------------------------------------
def test_list_paginates_on_next_page_token():
    """请求参数叫 page_token,响应字段叫 next_page_token —— 名字不对称。"""
    client, drive = make_client(
        FakeDrive(pages={ROOT: [[FakeFile("fld-1", "2026-08", "folder")],
                               [FakeFile("fld-2", "2026-09", "folder")]]})
    )
    drive.nodes = {}  # 只走 pages 分支

    entries = client._list_children(ROOT)

    assert [e.name for e in entries] == ["2026-08", "2026-09"]
    assert [call[1] for call in drive.list_calls] == [None, "1"]


def test_check_access_lists_root():
    client, drive = make_client()

    client.check_access()

    assert drive.list_calls == [(ROOT, None)]
    # 自检只问「列得动吗」,别顺手拉一整页
    assert drive.page_sizes == [1]


def test_check_access_maps_failure():
    client, _ = make_client(FakeDrive(code=99991672, msg="no scope"))

    with pytest.raises(DriveError) as exc:
        client.check_access()

    assert "99991672" in str(exc.value)


# ----------------------------------------------------------------------
# 约束:只用三种操作(不做删除/移动/权限)
# ----------------------------------------------------------------------
def test_only_list_create_and_upload_are_ever_used():
    client, drive = make_client()
    client.check_access()
    client.publish(
        ("2026-09", "leaf"),
        [upload("a.jpg"), upload("a.jpg")],
    )
    client.publish(("2026-09", "leaf"), [upload("a.jpg")])

    assert set(drive.requests) <= {"list", "create_folder", "upload_all"}


# ----------------------------------------------------------------------
# 落盘名规划(纯函数)
# ----------------------------------------------------------------------
def test_plan_names_sanitizes_path_separators_and_control_chars():
    assert plan_names(["a/b.jpg"]) == ["a_b.jpg"]
    assert plan_names(["a\\b.jpg"]) == ["a_b.jpg"]
    assert plan_names(["a\nb.jpg"]) == ["a_b.jpg"]


def test_plan_names_strips_whitespace_and_dots():
    assert plan_names(["  a.jpg  "]) == ["a.jpg"]
    assert plan_names(["a.jpg."]) == ["a.jpg"]


def test_plan_names_is_case_insensitive_about_collisions():
    """大小写不同也加后缀:宁可多一个后缀,也不要静默覆盖。"""
    assert plan_names(["A.jpg", "a.jpg"]) == ["A.jpg", "a (2).jpg"]


def test_plan_names_handles_extensionless_and_dotfiles():
    assert plan_names(["README", "README"]) == ["README", "README (2)"]
    assert plan_names([".gitignore", ".gitignore"]) == [
        ".gitignore",
        ".gitignore (2)",
    ]


def test_plan_names_suffix_does_not_collide_with_a_real_name():
    assert plan_names(["log.txt", "log (2).txt", "log.txt"]) == [
        "log.txt",
        "log (2).txt",
        "log (3).txt",
    ]


def test_plan_names_truncates_but_keeps_extension():
    planned = plan_names(["x" * 500 + ".jpg"])

    assert len(planned[0]) <= 200
    assert planned[0].endswith(".jpg")


def test_plan_names_is_deterministic():
    names = ["b.jpg", "a.jpg", "b.jpg"]

    assert plan_names(names) == plan_names(names)


def test_plan_names_rejects_blank_names():
    with pytest.raises(PermanentError):
        plan_names(["..."])
