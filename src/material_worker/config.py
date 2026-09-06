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
            poll_interval=int(os.getenv("POLL_INTERVAL", "5")),
            max_retries=int(os.getenv("MAX_RETRIES", "5")),
        )
