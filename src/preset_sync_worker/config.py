from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    feishu_app_id: str
    feishu_app_secret: str
    feishu_app_token: str
    feishu_table_id: str
    git_repository_url: str
    git_access_token: str
    #: 过程记录上传的云文档根目录。**留空 = 关闭上传**,过程记录退回只列文件名。
    feishu_drive_folder_token: str = ""
    #: 单文件上传上限(upload_all 的接口硬限制;更大的文件需要分片上传,未实现)
    feishu_drive_max_file_bytes: int = 20 * 1024 * 1024
    #: 拼云文档链接用的租户域名(`.env` 里历来没有这一项)
    feishu_host: str = "jfpolymers.feishu.cn"
    poll_interval: int = 5
    max_retries: int = 5

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()

        return cls(
            feishu_app_id=os.environ["FEISHU_APP_ID"],
            feishu_app_secret=os.environ["FEISHU_APP_SECRET"],
            feishu_app_token=os.environ["FEISHU_APP_TOKEN"],
            feishu_table_id=os.environ["FEISHU_TABLE_ID"],
            git_repository_url=os.environ["GIT_REPOSITORY_URL"],
            git_access_token=os.environ["GIT_ACCESS_TOKEN"],
            feishu_drive_folder_token=os.getenv(
                "FEISHU_DRIVE_FOLDER_TOKEN", ""
            ),
            feishu_drive_max_file_bytes=int(
                os.getenv(
                    "FEISHU_DRIVE_MAX_FILE_BYTES", str(20 * 1024 * 1024)
                )
            ),
            feishu_host=os.getenv("FEISHU_HOST", "jfpolymers.feishu.cn"),
            poll_interval=int(os.getenv("POLL_INTERVAL", "5")),
            max_retries=int(os.getenv("MAX_RETRIES", "5")),
        )
