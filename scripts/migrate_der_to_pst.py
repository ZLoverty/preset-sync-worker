"""旧表(DER「Preset Dev」)→ pst 的一次性数据搬运。

背景:旧表由另一个团队维护,管理方式与新的 pst + git 流程不符,但数据本身
要留着,所以做一次性搬运 —— **不走 worker 的提交/审核链路**,直接建行。

只搬四样东西,其余列一概不搬:
  1. 身份   Printer(文本)→ 打印机型号(单选);PI code(文本)→ PI Code(单选)
  2. 内容   `preset data` 列的 json → Profile JSON extract
  3. 证据   其余 11 个附件列 → 过程记录(11 列合并进 1 个附件列)

**只搬 Status=完成 的行**(`--status` 可改,`--status all` 关掉)。旧表 866 行里
完成 397 行、暂停/取消 256 行、待做 171 行 —— 没做完的搬过去只是噪音。

写进去的行:`状态=草稿`、`已请求` 留空 —— 它们是历史数据,不能被 worker 捡走,
也不该占着一个工作流状态。

三个实测出来的坑(不是推测):

- **单选列写入未定义的选项会失败**(`1254062 SingleSelectFieldConvFail`),
  不会自动建选项。所以源表里那 17 个 pst 还没有的 PI code,必须先由
  `--add-pi-options` 补进字段定义,再搬数据。
- **`PI Code` 是 pst 的索引列(字段列表第 0 位)**,改它的选项要原样回传
  已有选项(含 id/color),不能只发新增的那几个,否则等于重写整个字段。
- **附件上传的 parent_node 要的是「真实 app_token」,而它未必等于 .env 里那个**。
  `.env` 的 `FEISHU_APP_TOKEN=Jwncw7…` 是**知识库 wiki 节点 token**:pst 挂在
  知识库下,而 bitable 的记录接口会自动把 wiki token 解析成真实 token,所以
  读写记录一路正常;但**上传素材接口不做这个解析**,直接报
  `1061044 parent node not exist`——看着像权限问题,其实是拿错了 token。
  真实 app_token 要向 base 元信息接口问(`data.app.app_token` = `BZvNbk…`),
  见 `resolve_upload_node()`。
- `parent_type` 必须是 `bitable_file`(云空间的 `explorer` 是给目录用的,
  见 adapters/lark_drive.py,不是给附件列用的)。

用法::

    # 看一眼计划,不碰网络上的任何写操作
    python scripts/migrate_der_to_pst.py --record-id recvXXX --dry-run

    # 真搬一行
    python scripts/migrate_der_to_pst.py --record-id recvXXX

    # 先把缺的 PI code 选项补进 pst 字段
    python scripts/migrate_der_to_pst.py --add-pi-options

    # 批量(可反复跑,已搬过的按 PI Code×打印机型号 跳过)
    python scripts/migrate_der_to_pst.py --limit 50
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Sequence

import lark_oapi as lark
from dotenv import load_dotenv
from lark_oapi.api.bitable.v1 import (
    AppTableRecord,
    CreateAppTableRecordRequest,
    GetAppRequest,
    ListAppTableRecordRequest,
)
from lark_oapi.api.drive.v1 import (
    DownloadMediaRequest,
    UploadAllMediaRequest,
    UploadAllMediaRequestBody,
)

# ----------------------------------------------------------------------
# 映射表(源表取值 -> pst 选项),两边都是人工维护的枚举
# ----------------------------------------------------------------------

#: 源表 Printer 有 17 种取值,pst「打印机型号」正好 17 个选项,一一对应。
#: 差异只在写法:Bambu Lab 官方缩写是 BBL,Prusa 那项是大小写,
#: Anycubic 两台在源表里省掉了 "Kobra"。
PRINTER_MAP: dict[str, str] = {
    "Bambu P2S": "BBL P2S",
    "Bambu A1": "BBL A1",
    "Bambu H2D": "BBL H2D",
    "Bambu H2S": "BBL H2S",
    "Bambu H2C": "BBL H2C",
    "Bambu X2D": "BBL X2D",
    "Bambu A2L": "BBL A2L",
    "Bambu P1S": "BBL P1S",
    "Prusa core one": "Prusa Core One",
    "Anycubic S1": "Anycubic Kobra S1",
    "Anycubic KX": "Anycubic Kobra X",
    # 以下 6 个两边写法完全一致,列出来是为了让这张表自解释(改源表时不用去猜)
    "Elegoo CC2": "Elegoo CC2",
    "Snapmaker U1": "Snapmaker U1",
    "Creality K2 Pro": "Creality K2 Pro",
    "Creality I7": "Creality I7",
    "QIDI Q2": "QIDI Q2",
    "QIDI PLUS4": "QIDI PLUS4",
}

#: 进「过程记录」列的 11 个附件列(顺序 = 上传顺序,决定列内展示次序)
PROCESS_RECORD_COLUMNS: tuple[str, ...] = (
    "冷却参数照片",
    "温度塔照片",
    "最大体积流量照片",
    "流量比例照片",
    "PA校准照片",
    "小船45°模型",
    "综合模型45°",
    "综合模型底部",
    "面包机照片",
    "回抽测试",
    "preset 3mf",
)

#: 进「Profile JSON extract」列的附件列(存的可能是 .json,也可能是 .ini)
PROFILE_JSON_COLUMN = "preset data"

SRC_PRINTER = "Printer"
SRC_PI = "PI code"
#: 源表 Status(单选:待做 / 进行中 / 待审核 / 完成 / 暂停·取消)。
#: 旧表 866 行里只有 397 行是「完成」,其余是没做完或已放弃的 —— 搬过去
#: 只会给 pst 添噪音,所以默认只搬完成的行。
SRC_STATUS = "Status"
SRC_STATUS_DONE = "完成"

DST_PRINTER = "打印机型号"
DST_PI = "PI Code"
DST_STATUS = "状态"
DST_PROCESS = "过程记录"
DST_PROFILE = "Profile JSON extract"
DRAFT = "草稿"

BITABLE_FILE = "bitable_file"
PAGE_SIZE = 500


# ----------------------------------------------------------------------
# 传输
# ----------------------------------------------------------------------
class MigrateError(RuntimeError):
    """搬运过程中的失败(带接口错误码,便于人对号入座)。"""


def _call(fn, what: str):
    """一次 SDK 调用 + 失败归一化(与 worker 的 call_lark 同语义)。"""
    try:
        resp = fn()
    except Exception as exc:  # 传输层
        raise MigrateError(f"{what} 请求失败: {type(exc).__name__}: {exc}") from exc
    if not resp.success():
        raise MigrateError(f"{what} 失败: code={resp.code}, msg={resp.msg}")
    return resp


def build_client() -> lark.Client:
    return (
        lark.Client.builder()
        .app_id(os.environ["FEISHU_APP_ID"])
        .app_secret(os.environ["FEISHU_APP_SECRET"])
        .build()
    )


def list_records(client: lark.Client, app: str, table: str):
    """全表分页拉取,产出 (record_id, fields)。"""
    rows: list[tuple[str, dict]] = []
    page_token: str | None = None
    while True:
        builder = (
            ListAppTableRecordRequest.builder()
            .app_token(app)
            .table_id(table)
            .page_size(PAGE_SIZE)
        )
        if page_token:
            builder.page_token(page_token)
        resp = _call(
            lambda: client.bitable.v1.app_table_record.list(builder.build()),
            "list records",
        )
        rows.extend((it.record_id, it.fields or {}) for it in (resp.data.items or []))
        if not resp.data.has_more:
            return rows
        page_token = resp.data.page_token


def download_media(client: lark.Client, file_token: str, what: str) -> bytes:
    resp = _call(
        lambda: client.drive.v1.media.download(
            DownloadMediaRequest.builder().file_token(file_token).build()
        ),
        f"下载 {what}",
    )
    if resp.file is None:
        raise MigrateError(f"下载 {what} 失败: 响应缺少文件内容")
    return resp.file.read()


def resolve_upload_node(client: lark.Client, app_token: str) -> str:
    """把 .env 里的 token 换成上传素材接口认的**真实 app_token**。

    pst 挂在知识库下时,`.env` 里那个是 wiki 节点 token。记录接口会替你解析,
    上传素材接口不会 —— 拿 wiki token 当 parent_node 就是
    `1061044 parent node not exist`。这里向 base 元信息问一次真实值。
    """
    resp = _call(
        lambda: client.bitable.v1.app.get(
            GetAppRequest.builder().app_token(app_token).build()
        ),
        "get app",
    )
    real = getattr(resp.data.app, "app_token", None)
    if not real:
        raise MigrateError("base 元信息没返回 app_token")
    return str(real)


def upload_attachment(
    client: lark.Client, upload_node: str, name: str, content: bytes
) -> str:
    """把字节传成目标表的一个附件,返回 file_token。

    `upload_node` 必须是**真实 app_token**(见 `resolve_upload_node`)。

    `file` 必须是 io.BytesIO:SDK 的 multipart 序列化只放行 IOBase 与 tuple,
    传裸 bytes 会把字面量 `b'...'` 塞进表单(同 adapters/lark_drive.py 的坑)。
    """
    body = (
        UploadAllMediaRequestBody.builder()
        .file_name(name)
        .parent_type(BITABLE_FILE)
        .parent_node(upload_node)
        .size(len(content))
        .file(io.BytesIO(content))
        .build()
    )
    resp = _call(
        lambda: client.drive.v1.media.upload_all(
            UploadAllMediaRequest.builder().request_body(body).build()
        ),
        f"上传 {name}",
    )
    token = getattr(resp.data, "file_token", None)
    if not token:
        raise MigrateError(f"上传 {name} 未返回 file_token")
    return str(token)


# ----------------------------------------------------------------------
# 源行 -> 目标字段
# ----------------------------------------------------------------------
@dataclass
class Attachment:
    """源表附件格子里的一项,带上它来自哪一列。"""

    column: str
    file_token: str
    name: str
    size: int


def attachments_of(row: dict, column: str) -> list[Attachment]:
    cell = row.get(column)
    if not isinstance(cell, list):
        return []
    out: list[Attachment] = []
    for entry in cell:
        if not isinstance(entry, dict):
            continue
        token = entry.get("file_token")
        if not token:
            continue
        size = entry.get("size")
        out.append(
            Attachment(
                column=column,
                file_token=str(token),
                name=str(entry.get("name") or ""),
                size=size if isinstance(size, int) and not isinstance(size, bool) else 0,
            )
        )
    return out


def text_of(row: dict, column: str) -> str:
    """源表文本列取值(飞书可能返回字符串,也可能返回 [{text:...}] 富文本)。"""
    value = row.get(column)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [
            str(e.get("text", ""))
            for e in value
            if isinstance(e, dict) and e.get("text")
        ]
        return "".join(parts).strip()
    return ""


def uploaded_names(atts: Sequence[Attachment], mode: str) -> list[str]:
    """一批附件 -> 落盘名(整批一次算完,因为序号要按**列内**计数)。

    源表的照片几乎全叫「媒体 (3).jpg」——编号是上传批次给的,不带任何语义,
    直接搬进 pst 就没法分辨哪张是哪个测试。默认把来源列名拼在前面
    (如 `冷却参数照片_媒体 (3).jpg`),`--name-mode original` 可关掉。

    只有**同一列里不止一个附件**时才加序号:跨列计数会把每一张都加上
    `(1)` `(2)` … 变成噪音。
    """
    per_column = Counter(a.column for a in atts)
    seen: Counter = Counter()
    names: list[str] = []
    for att in atts:
        seen[att.column] += 1
        original = att.name.strip() or f"{att.column}.bin"
        if mode == "original":
            names.append(original)
            continue
        name = f"{att.column}_{original}"
        if per_column[att.column] > 1:
            stem, dot, ext = name.rpartition(".")
            suffix = f" ({seen[att.column]})"
            name = f"{stem}{suffix}{dot}{ext}" if dot else f"{name}{suffix}"
        names.append(name)
    return names


@dataclass
class RowPlan:
    """一行要搬什么(不碰网络,便于 --dry-run 先看)。"""

    record_id: str
    pi_code: str
    printer_src: str
    printer_dst: str
    profile_json: list[Attachment]
    process_record: list[Attachment]
    problems: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(a.size for a in self.profile_json + self.process_record)


def plan_row(row: dict, record_id: str) -> RowPlan:
    pi_code = text_of(row, SRC_PI)
    printer_src = text_of(row, SRC_PRINTER)
    printer_dst = PRINTER_MAP.get(printer_src, "")

    problems: list[str] = []
    if not pi_code:
        problems.append("源行没有 PI code")
    if not printer_src:
        problems.append("源行没有 Printer")
    elif not printer_dst:
        problems.append(f"Printer「{printer_src}」没有映射到 pst 选项")

    return RowPlan(
        record_id=record_id,
        pi_code=pi_code,
        printer_src=printer_src,
        printer_dst=printer_dst,
        profile_json=attachments_of(row, PROFILE_JSON_COLUMN),
        process_record=[
            att
            for col in PROCESS_RECORD_COLUMNS
            for att in attachments_of(row, col)
        ],
        problems=problems,
    )


def existing_keys(client: lark.Client, app: str, table: str) -> set[tuple[str, str]]:
    """pst 里已有的 (PI Code, 打印机型号),用于跳过已搬过的行。"""
    keys: set[tuple[str, str]] = set()
    for _rid, fields in list_records(client, app, table):
        keys.add((_plain(fields.get(DST_PI)), _plain(fields.get(DST_PRINTER))))
    return keys


def _plain(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return str(value[0].get("text", "") or "")
    if isinstance(value, dict):
        return str(value.get("text", "") or "")
    return "" if value is None else str(value)


# ----------------------------------------------------------------------
# PI Code 选项补齐
# ----------------------------------------------------------------------
def _fields_raw(client: lark.Client, app: str, table: str) -> list[dict]:
    resp = _call(
        lambda: client.request(
            lark.BaseRequest.builder()
            .http_method(lark.HttpMethod.GET)
            .uri(f"/open-apis/bitable/v1/apps/{app}/tables/{table}/fields")
            .token_types({lark.AccessTokenType.TENANT})
            .queries([("page_size", "100")])
            .build()
        ),
        "list fields(raw)",
    )
    import json

    return json.loads(resp.raw.content)["data"]["items"]


def pi_options(client: lark.Client, dst_app: str, dst_table: str) -> set[str]:
    """pst「PI Code」现有的选项名集合(用于**上传前**预检)。

    单选列写入未定义的选项会 `1254062`,而失败发生在建行那一步 —— 也就是
    附件已经全部下载并上传完之后。批量跑时这等于白烧一遍流量(7GB 级别),
    所以先把选项读出来,不合格的行根本不进上传流程。
    """
    field_def = next(
        (f for f in _fields_raw(client, dst_app, dst_table) if f["field_name"] == DST_PI),
        None,
    )
    if field_def is None:
        raise MigrateError(f"pst 里没有「{DST_PI}」列")
    return {
        o["name"]
        for o in (field_def.get("property") or {}).get("options") or []
        if o.get("name")
    }


def add_pi_options(
    client: lark.Client, dst_app: str, dst_table: str, wanted: set[str]
) -> list[str]:
    """把 pst「PI Code」里缺的选项补上,返回实际新增的选项名。

    原样回传已有选项(连 id/color 一起)—— 这是**索引列**,只发新增的那几个
    等于重写整个字段的定义。
    """
    import json

    field_def = next(
        (f for f in _fields_raw(client, dst_app, dst_table) if f["field_name"] == DST_PI),
        None,
    )
    if field_def is None:
        raise MigrateError(f"pst 里没有「{DST_PI}」列")

    options = list((field_def.get("property") or {}).get("options") or [])
    have = {o["name"] for o in options}
    missing = sorted(wanted - have)
    if not missing:
        return []

    payload = {
        "field_name": DST_PI,
        "type": field_def["type"],
        "property": {"options": options + [{"name": n} for n in missing]},
    }
    resp = _call(
        lambda: client.request(
            lark.BaseRequest.builder()
            .http_method(lark.HttpMethod.PUT)
            .uri(
                f"/open-apis/bitable/v1/apps/{dst_app}/tables/{dst_table}"
                f"/fields/{field_def['field_id']}"
            )
            .token_types({lark.AccessTokenType.TENANT})
            .body(payload)
            .build()
        ),
        "update PI Code options",
    )
    got = {
        o["name"]
        for o in (json.loads(resp.raw.content)["data"]["field"].get("property") or {})
        .get("options", [])
    }
    return sorted(wanted & got - have)


# ----------------------------------------------------------------------
# 搬一行
# ----------------------------------------------------------------------
def migrate_row(
    client: lark.Client,
    plan: RowPlan,
    dst_app: str,
    dst_table: str,
    upload_node: str,
    name_mode: str,
) -> str:
    """下载 → 上传 → 建行,返回新记录的 record_id。"""
    profile_names = uploaded_names(plan.profile_json, name_mode)
    profile_tokens: list[str] = []
    for att, name in zip(plan.profile_json, profile_names):
        print(f"    ↓ {att.column}/{att.name} ({att.size/1024:.0f}KB)")
        content = download_media(client, att.file_token, att.name)
        profile_tokens.append(upload_attachment(client, upload_node, name, content))

    process_names = uploaded_names(plan.process_record, name_mode)
    process_tokens: list[str] = []
    for att, name in zip(plan.process_record, process_names):
        print(f"    ↓ {att.column}/{att.name} ({att.size/1024:.0f}KB)")
        content = download_media(client, att.file_token, att.name)
        process_tokens.append(upload_attachment(client, upload_node, name, content))

    fields: dict = {
        DST_PI: plan.pi_code,
        DST_PRINTER: plan.printer_dst,
        DST_STATUS: DRAFT,
        # 已请求 不写 —— 留空,worker 不会捡走历史数据
    }
    if profile_tokens:
        fields[DST_PROFILE] = [{"file_token": t} for t in profile_tokens]
    if process_tokens:
        fields[DST_PROCESS] = [{"file_token": t} for t in process_tokens]

    body = AppTableRecord.builder().fields(fields).build()
    resp = _call(
        lambda: client.bitable.v1.app_table_record.create(
            CreateAppTableRecordRequest.builder()
            .app_token(dst_app)
            .table_id(dst_table)
            .request_body(body)
            .build()
        ),
        "create record",
    )
    return str(resp.data.record.record_id)


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--record-id", nargs="+", help="只搬这几行(源表 record_id,可给多个)"
    )
    parser.add_argument("--limit", type=int, help="最多搬多少行")
    parser.add_argument(
        "--status",
        default=SRC_STATUS_DONE,
        help=f"只搬源表 Status = 该值的行(默认「{SRC_STATUS_DONE}」;all = 不筛)",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印计划,不写")
    parser.add_argument(
        "--add-pi-options", action="store_true", help="把缺的 PI Code 选项补进 pst 字段"
    )
    parser.add_argument(
        "--name-mode",
        choices=("prefixed", "original"),
        default="prefixed",
        help="附件落盘名:prefixed=拼上来源列名(默认),original=保持源表原名",
    )
    parser.add_argument(
        "--sleep", type=float, default=0.2, help="行间间隔秒数(批量时防限流)"
    )
    parser.add_argument("--env", default=None, help=".env 路径(默认当前目录)")
    args = parser.parse_args(argv)

    load_dotenv(args.env)
    src_app, src_table = os.environ["app_token"], os.environ["table_id"]
    dst_app, dst_table = (
        os.environ["FEISHU_APP_TOKEN"],
        os.environ["FEISHU_TABLE_ID"],
    )
    client = build_client()

    print(f"源: {src_app[:6]}…/{src_table}   →   目标: {dst_app[:6]}…/{dst_table}")

    # ---- 只补选项 ----
    if args.add_pi_options:
        wanted = {
            text_of(f, SRC_PI)
            for _rid, f in list_records(client, src_app, src_table)
        } - {""}
        added = add_pi_options(client, dst_app, dst_table, wanted)
        print(f"新增 {len(added)} 个 PI Code 选项: {'、'.join(added) if added else '(无)'}")
        return 0

    # ---- 挑行 ----
    rows = list_records(client, src_app, src_table)
    if args.record_id:
        wanted = set(args.record_id)
        rows = [(rid, f) for rid, f in rows if rid in wanted]
        missing = wanted - {rid for rid, _ in rows}
        if missing:
            print(f"源表里没有这些 record_id: {'、'.join(sorted(missing))}")
            return 1
        # 显式点名 = 覆盖状态筛选(与跳过「已搬过」的规则一致:人工指定优先)
    elif args.status != "all":
        kept = [
            (rid, f) for rid, f in rows if text_of(f, SRC_STATUS) == args.status
        ]
        print(
            f"源表 {len(rows)} 行,Status={args.status} 的 {len(kept)} 行进入搬运"
            f"(其余 {len(rows) - len(kept)} 行跳过)"
        )
        rows = kept

    # 上传前预检:pst 的 PI Code 选项里没有的一律不搬 —— 建行才会报 1254062,
    # 那时附件已经白下白传了一遍(见 pi_options 的注释)。
    dst_pi_options = pi_options(client, dst_app, dst_table)
    known = existing_keys(client, dst_app, dst_table)
    plans: list[RowPlan] = []
    blocked: list[RowPlan] = []
    for rid, fields in rows:
        plan = plan_row(fields, rid)
        if plan.problems:
            print(f"  跳过 {rid}: {'; '.join(plan.problems)}")
            continue
        if plan.pi_code not in dst_pi_options:
            blocked.append(plan)
            continue
        if (plan.pi_code, plan.printer_dst) in known and not args.record_id:
            print(f"  跳过 {rid}: pst 已有 ({plan.pi_code}, {plan.printer_dst})")
            continue
        plans.append(plan)
        if args.limit and len(plans) >= args.limit:
            break

    for plan in blocked:
        print(
            f"  跳过 {plan.record_id}: pst「{DST_PI}」没有选项「{plan.pi_code}」"
            f"(附件 {plan.total_bytes / 1024 / 1024:.1f}MB 一个都没下)"
        )

    # ---- 计划 ----
    print(f"\n待搬 {len(plans)} 行,附件合计 {sum(p.total_bytes for p in plans)/1024/1024:.1f}MB")
    for plan in plans:
        print(
            f"  {plan.record_id}  {plan.pi_code:<12} {plan.printer_src} → {plan.printer_dst}"
            f"   json×{len(plan.profile_json)}  过程记录×{len(plan.process_record)}"
            f"  {plan.total_bytes/1024/1024:.1f}MB"
        )

    if args.dry_run:
        print("\n(--dry-run,没有写任何东西)")
        return 0

    # ---- 执行 ----
    upload_node = resolve_upload_node(client, dst_app)
    if upload_node != dst_app:
        print(f"\n上传落点:.env 给的是 wiki token,已解析为真实 app_token {upload_node[:6]}…")

    done = 0
    for plan in plans:
        print(f"\n▶ {plan.record_id} {plan.pi_code} {plan.printer_dst}")
        try:
            new_id = migrate_row(
                client, plan, dst_app, dst_table, upload_node, args.name_mode
            )
        except MigrateError as exc:
            print(f"  ✗ {exc}")
            continue
        print(f"  ✓ 新建 pst 记录 {new_id}")
        done += 1
        if args.sleep:
            time.sleep(args.sleep)

    print(f"\n完成 {done}/{len(plans)} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
