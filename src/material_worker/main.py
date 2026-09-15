from __future__ import annotations

import logging

import lark_oapi as lark

from material_worker.adapters.bitable import BitableClient
from material_worker.adapters.git import GitRepository
from material_worker.adapters.lark_drive import DriveClient
from material_worker.config import Settings
from material_worker.logging_setup import configure_logging, log
from material_worker.services.submission_service import SubmissionService
from material_worker.worker import MaterialWorker


def create_drive(settings: Settings, lark_client: lark.Client) -> DriveClient | None:
    """过程记录上传用的云文档客户端;未配置根目录则返回 None(关闭上传)。

    这里只负责「建不建」,启动自检 `check_access()` 由调用方在 `create_worker`
    里显式触发 —— 权限/共享没配好要在**启动时**炸,而不是每一行烧 5 次重试
    才失败(400 行 × 5 次白跑,而且失败信息淹没在表格里)。
    """
    if not settings.feishu_drive_folder_token:
        # 「忘配」和「故意关」长得一样,所以必须大声 —— 否则过程记录会静默地
        # 退回「只留文件名、原件不留存」。
        log(
            "[配置] 未配置 FEISHU_DRIVE_FOLDER_TOKEN:"
            "过程记录只写文件名,不上传飞书云文档(原件不留存)"
        )
        return None

    return DriveClient(
        client=lark_client,
        root_folder_token=settings.feishu_drive_folder_token,
        host=settings.feishu_host,
        max_file_bytes=settings.feishu_drive_max_file_bytes,
    )


def create_worker(settings: Settings) -> MaterialWorker:
    lark_client = (
        lark.Client.builder()
        .app_id(settings.feishu_app_id)
        .app_secret(settings.feishu_app_secret)
        .build()
    )

    bitable = BitableClient(
        client=lark_client,
        app_token=settings.feishu_app_token,
        table_id=settings.feishu_table_id,
    )

    repository = GitRepository(
        repository_url=settings.git_repository_url,
        access_token=settings.git_access_token,
    )

    drive = create_drive(settings, lark_client)
    if drive is not None:
        # 启动自检:列一次根目录。应用缺 drive:drive 或文件夹没共享给应用
        # 都在这里立刻失败(与 boot() 里的 ensure_schema 同类检查)。
        drive.check_access()
        log(f"[Drive] 过程记录将存档到云文档根目录 {settings.feishu_drive_folder_token}")

    service = SubmissionService(
        bitable=bitable,
        repository=repository,
        drive=drive,
        max_retries=settings.max_retries,
    )

    return MaterialWorker(
        bitable=bitable,
        submission_service=service,
    )


def main() -> None:
    configure_logging()
    log("[启动] preset-sync-worker starting")
    settings = Settings.from_env()
    worker = create_worker(settings)

    log(
        f"开始监听：每 {settings.poll_interval}s 轮询 "
        f"(自动重试上限 {settings.max_retries} 次)"
    )

    worker.boot()
    worker.run_forever(settings.poll_interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("[停止] 收到中断信号，worker 正常退出")
    except Exception:
        logging.getLogger(__name__).exception("[启动失败] worker 无法启动")
        raise SystemExit(1)
