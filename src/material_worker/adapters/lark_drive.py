"""飞书云空间(drive.v1)—— 过程记录的长期存档。

只做三件事:**列目录、建目录、上传**。不做删除、不做移动、不改权限 ——
目标目录是只增不删的证据仓库,不是一个文件管理器。(`drive:drive` 权限足够
做删除,是这里**不写**,不是做不到。)

三个改之前必须先看的约定:

1. `upload_all` 的 `file` **必须传 `io.BytesIO`,不能传裸 `bytes`**。
   SDK 的 `parse_form_data` 只放行 `io.IOBase` 与 `tuple`,其余一律 `str(v)`
   —— 传 bytes 会把字面量 `b'...'` 塞进 multipart 表单,服务端直接拒绝。
2. `ListFileRequest` 的**请求参数**叫 `page_token`,**响应字段**却叫
   `next_page_token`,名字不对称。
3. `create_folder` **不幂等且不支持并发** —— 必须先列后建。

业务逻辑只调用本文件的方法,不直接接触 lark_oapi(与 BitableClient 同规矩)。
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Any, Sequence

import lark_oapi as lark
from lark_oapi.api.drive.v1 import (
    CreateFolderFileRequest,
    CreateFolderFileRequestBody,
    ListFileRequest,
    UploadAllFileRequest,
    UploadAllFileRequestBody,
)

from material_worker.adapters.lark_transport import call_lark
from material_worker.exceptions import DriveError, PermanentError

#: 云空间节点类型(ListFile 返回的 type 字段)
FOLDER_TYPE = "folder"
FILE_TYPE = "file"
#: 上传的父节点类型:云空间文件夹
EXPLORER = "explorer"

#: `upload_all` 单文件上限(飞书接口硬限制)。更大的文件要走分片上传,本适配器不做。
DEFAULT_MAX_FILE_BYTES = 20 * 1024 * 1024
#: 列目录分页大小
LIST_PAGE_SIZE = 200
#: 落盘文件名长度上限(飞书限制约 250,留足余量)
NAME_MAX = 200

DEFAULT_HOST = "jfpolymers.feishu.cn"

#: 文件名里不允许出现的字符(路径分隔符 + 控制字符)
_ILLEGAL_NAME_CHARS = re.compile(r"[/\\\x00-\x1f\x7f]")


@dataclass(frozen=True)
class DriveUpload:
    """一个待上传的文件:展示名 + 原始字节。"""

    name: str
    content: bytes


@dataclass(frozen=True)
class DriveEntry:
    """目录里的一项。

    只留**判断与复用真正用得到**的字段:type(区分同名文件/文件夹)、
    name(按名跳过)、token(复用目录)。
    """

    name: str
    type: str
    token: str


@dataclass(frozen=True)
class DrivePublishResult:
    """一次发布的落点。"""

    folder_token: str
    folder_url: str
    uploaded: int
    skipped: int


class DriveClient:
    """飞书云空间的唯一 SDK 交互入口。"""

    def __init__(
        self,
        client: lark.Client,
        root_folder_token: str,
        *,
        host: str = DEFAULT_HOST,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ):
        self.client = client
        self.root_folder_token = root_folder_token
        self.host = host.strip().rstrip("/")
        self.max_file_bytes = max_file_bytes

    # ------------------------------------------------------------------
    # 启动自检
    # ------------------------------------------------------------------
    def check_access(self) -> None:
        """列一次根目录,确认应用真的能读写它。

        放在进程启动时跑:权限没开 / 目录没共享给应用,这里就失败 ——
        否则要等每一行烧完 5 次重试才在表格里失败,原因还散落在各处。
        """
        self._list_children(self.root_folder_token, limit=1)

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------
    def publish(
        self,
        folder_path: Sequence[str],
        uploads: Sequence[DriveUpload],
    ) -> DrivePublishResult:
        """把 uploads 发布到 root/<folder_path...>/ 下,返回落点与计数。

        **幂等**:路径逐层先查后建,目录内按名跳过。整轮重试(含进程重启)
        会算出同样的路径与同样的落盘名,所以重复调用不会产生重复文件,
        跳过的文件也不会丢 —— 它们本来就在那儿。
        """
        # 先把所有校验做完再碰网络:避免传了一半才发现某个文件不合格
        planned = plan_names([u.name for u in uploads])
        self._check_sizes(uploads)

        if not uploads:
            # 没有附件也要保证目录存在(空目录 = 本轮确实没有记录)
            folder_token = self._ensure_path(folder_path)
            return DrivePublishResult(
                folder_token, self._folder_url(folder_token), 0, 0
            )

        folder_token = self._ensure_path(folder_path)
        existing = {
            entry.name
            for entry in self._list_children(folder_token)
            if entry.type == FILE_TYPE
        }

        uploaded = 0
        skipped = 0
        for upload, name in zip(uploads, planned):
            if name in existing:
                skipped += 1
                continue
            self._upload(folder_token, name, upload.content)
            uploaded += 1

        return DrivePublishResult(
            folder_token, self._folder_url(folder_token), uploaded, skipped
        )

    # ------------------------------------------------------------------
    # 目录
    # ------------------------------------------------------------------
    def _ensure_path(self, folder_path: Sequence[str]) -> str:
        token = self.root_folder_token
        for segment in folder_path:
            token = self._ensure_folder(token, segment)
        return token

    def _ensure_folder(self, parent_token: str, name: str) -> str:
        """取(必要时创建)parent 下名为 name 的文件夹,返回其 token。

        必须先列后建:create_folder 既不幂等也不支持并发,直接建会造出
        同名文件夹并把文件分裂到两处。比对时**连 type 一起比** —— 否则
        父目录下一个同名的文件会顶替掉本该复用的文件夹。
        """
        for entry in self._list_children(parent_token):
            if entry.type == FOLDER_TYPE and entry.name == name:
                return entry.token

        body = (
            CreateFolderFileRequestBody.builder()
            .name(name)
            .folder_token(parent_token)
            .build()
        )
        request = CreateFolderFileRequest.builder().request_body(body).build()
        resp = call_lark(
            lambda: self.client.drive.v1.file.create_folder(request),
            f"create folder {name}",
            surface="Drive",
            error_cls=DriveError,
        )
        token = getattr(resp.data, "token", None)
        if not token:
            raise DriveError(f"新建云文档目录「{name}」未返回 token")
        return str(token)

    def _list_children(
        self, folder_token: str, *, limit: int | None = None
    ) -> list[DriveEntry]:
        entries: list[DriveEntry] = []
        page_token: str | None = None

        while True:
            builder = (
                ListFileRequest.builder()
                .folder_token(folder_token)
                # 只要 1 条时别顺手拉 200 条(启动自检就走这条路)
                .page_size(
                    LIST_PAGE_SIZE if limit is None else min(LIST_PAGE_SIZE, limit)
                )
            )
            if page_token:
                builder.page_token(page_token)

            resp = call_lark(
                lambda: self.client.drive.v1.file.list(builder.build()),
                "list files",
                surface="Drive",
                error_cls=DriveError,
            )
            entries.extend(_to_entries(resp.data))

            if limit is not None and len(entries) >= limit:
                return entries[:limit]
            # 注意:请求参数是 page_token,响应字段是 next_page_token
            if not getattr(resp.data, "has_more", False):
                return entries
            page_token = getattr(resp.data, "next_page_token", None)
            if not page_token:
                return entries

    # ------------------------------------------------------------------
    # 上传
    # ------------------------------------------------------------------
    def _upload(self, parent_token: str, name: str, content: bytes) -> None:
        body = (
            UploadAllFileRequestBody.builder()
            .file_name(name)
            .parent_type(EXPLORER)
            .parent_node(parent_token)
            .size(len(content))
            # 必须 IOBase:裸 bytes 会被 SDK 序列化成字面量 b'...'(见模块 docstring)
            .file(io.BytesIO(content))
            .build()
        )
        request = UploadAllFileRequest.builder().request_body(body).build()
        call_lark(
            lambda: self.client.drive.v1.file.upload_all(request),
            f"upload {name}",
            surface="Drive",
            error_cls=DriveError,
        )

    def _check_sizes(self, uploads: Sequence[DriveUpload]) -> None:
        """体积预检。放在建目录之前 —— 不合格的批次一个请求都不该发。"""
        for upload in uploads:
            size = len(upload.content)
            if size > self.max_file_bytes:
                raise PermanentError(
                    f"过程记录「{upload.name}」{_mb(size)} 超过飞书单文件上传上限 "
                    f"{_mb(self.max_file_bytes)},请压缩后重新上传该附件"
                )

    def _folder_url(self, folder_token: str) -> str:
        return f"https://{self.host}/drive/folder/{folder_token}"


# ----------------------------------------------------------------------
# 落盘名规划(纯函数,可单测)
# ----------------------------------------------------------------------
def plan_names(names: Sequence[str]) -> list[str]:
    """把原始文件名排成一组**确定性**的落盘名。

    清洗非法字符、批内重名追加 " (2)" " (3)"。确定性是幂等的前提 ——
    重试必须算出同样的结果,否则目录内「按名跳过」就失效了。
    """
    planned: list[str] = []
    taken: set[str] = set()

    for raw in names:
        base = _truncate(_sanitize(raw))
        if not base:
            raise PermanentError(
                f"过程记录里有一个附件没有可用的文件名({str(raw)!r}),"
                f"请重命名后重新上传"
            )
        candidate = base
        index = 2
        # 后缀可能撞上批内已有的真名(如本来就有一个 "log (2).txt")
        while candidate.casefold() in taken:
            candidate = _with_suffix(base, index)
            index += 1
        taken.add(candidate.casefold())
        planned.append(candidate)

    return planned


def _sanitize(name: object) -> str:
    # 只去尾部点:开头点是合法的(如 .gitignore),而 ".." / "." 去掉后为空,
    # 会在 plan_names 里按「没有可用的文件名」处理
    text = _ILLEGAL_NAME_CHARS.sub("_", str(name))
    return text.strip().rstrip(".")


def _truncate(name: str, limit: int = NAME_MAX) -> str:
    if len(name) <= limit:
        return name
    stem, ext = _split_ext(name)
    if ext and limit - len(ext) > 0:
        return f"{stem[: limit - len(ext)]}{ext}"
    return name[:limit]


def _with_suffix(name: str, index: int) -> str:
    stem, ext = _split_ext(name)
    suffix = f" ({index})"
    keep = max(NAME_MAX - len(suffix) - len(ext), 1)
    return f"{stem[:keep]}{suffix}{ext}"


def _split_ext(name: str) -> tuple[str, str]:
    """拆成 (主名, 扩展名)。

    **开头的点不算扩展名分隔符** —— `.gitignore` 是主名,不是"扩展名 gitignore";
    否则加后缀会算出 ` (2).gitignore` 这种鬼东西。
    """
    index = name.rfind(".")
    if index <= 0:  # 没有点,或点在开头
        return name, ""
    ext = name[index + 1 :]
    if not ext or len(ext) > 16 or "/" in ext or "\\" in ext:
        return name, ""
    return name[:index], f".{ext}"


def _to_entries(data: Any) -> list[DriveEntry]:
    entries: list[DriveEntry] = []
    for item in getattr(data, "files", None) or []:
        token = getattr(item, "token", None)
        if not token:
            continue
        entries.append(
            DriveEntry(
                name=str(getattr(item, "name", "") or ""),
                type=str(getattr(item, "type", "") or ""),
                token=str(token),
            )
        )
    return entries


def _mb(size: int) -> str:
    return f"{size / 1024 / 1024:.1f}MB"
