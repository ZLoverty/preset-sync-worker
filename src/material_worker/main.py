from __future__ import annotations

import lark_oapi as lark

from material_worker.adapters.bitable import BitableClient
from material_worker.adapters.git import GitRepository
from material_worker.config import Settings
from material_worker.services.submission_service import SubmissionService
from material_worker.worker import MaterialWorker


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

    service = SubmissionService(
        bitable=bitable,
        repository=repository,
        max_retries=settings.max_retries,
    )

    return MaterialWorker(
        bitable=bitable,
        submission_service=service,
    )


def main() -> None:
    settings = Settings.from_env()
    worker = create_worker(settings)

    print(
        f"开始监听：每 {settings.poll_interval}s 轮询 "
        f"(自动重试上限 {settings.max_retries} 次)"
    )

    worker.boot()
    worker.run_forever(settings.poll_interval)


if __name__ == "__main__":
    main()
