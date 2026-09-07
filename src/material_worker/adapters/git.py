from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from material_worker.domain.profile import MaterialProfile
from material_worker.exceptions import (
    GitRepositoryError,
    PermanentError,
    RetryableError,
)

REQUEST_TIMEOUT_SECONDS = 30

# GitHub 对 PR list 的 head 过滤需要 owner:branch;Gitea 用 branch 名即可。
# 非 github.com 的 host 一律按 Gitea 兼容 API(/api/v1)处理。


@dataclass
class PullRequestResult:
    branch_name: str
    pull_request_url: str


class _HttpError(Exception):
    """非预期 HTTP 状态(4xx 而非鉴权/限流),由上层按场景解释。"""

    def __init__(self, status: int, payload: Any):
        super().__init__(f"HTTP {status}: {payload}")
        self.status = status
        self.payload = payload


class _PullRequestExists(_HttpError):
    """创建 PR 时远端返回「该 head 已有 PR」。"""


class GitRepository:
    """Git 提供方适配器(纯 HTTP API,无本地 clone)。

    - GitHub(api.github.com)与 Gitea(/api/v1)按 repository_url 自动识别;
    - Contents API 的一次 PUT 在远端原子地完成 write+commit+push,
      因此这里没有 git-CLI 意义上的独立 push 步骤:push() 只校验
      commit 已落到远端分支(见其 docstring);
    - 所有鉴权/API 细节都在本类,GitRepository 之外看不到任何 HTTP。

    环境要求: repository_url 必须是 https URL(需带 Access Token),
    形如 https://github.com/owner/repo(.git) 或自托管 Gitea 同构 URL。
    """

    def __init__(self, repository_url: str, access_token: str):
        self.access_token = access_token
        self._scheme, self._host, self._owner, self._repo = (
            self._parse_repository_url(repository_url)
        )
        if "github.com" in self._host:
            self.api_base = "https://api.github.com"
            self._github = True
        else:
            self.api_base = f"{self._scheme}://{self._host}/api/v1"
            self._github = False
        self._default_branch: str | None = None

    # ------------------------------------------------------------------
    # URL / 路径
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_repository_url(repository_url: str) -> tuple[str, str, str, str]:
        url = repository_url.strip()
        if url.startswith("git@") or url.startswith("ssh://"):
            raise PermanentError(
                "GIT_REPOSITORY_URL 为 SSH 形式;HTTP API 模式需要 https URL"
            )
        if "://" not in url:
            url = f"https://{url}"
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https"):
            raise PermanentError(
                f"不支持的 GIT_REPOSITORY_URL scheme: {parsed.scheme}"
            )
        path = parsed.path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        segments = [s for s in path.split("/") if s]
        if len(segments) != 2:
            raise PermanentError(
                f"无法从 GIT_REPOSITORY_URL 解析 owner/repo: {repository_url}"
            )
        host = parsed.netloc
        if "@" in host:  # 形如 user@host —— HTTP 模式不需要用户名
            host = host.rsplit("@", 1)[1]
        return parsed.scheme, host, segments[0], segments[1]

    @staticmethod
    def branch_name_for(submission_id: str) -> str:
        """branch 名由 submission_id 唯一确定(P4 #20,幂等的基础)。"""
        return f"material/{submission_id}"

    def profile_path(self, profile: MaterialProfile) -> str:
        """档案文件在仓库中的路径(纯函数,不触网)。

        V2-P4:布局 = 照搬 Polymaker-Preset 实况(域内派生,见
        MaterialProfile.repo_relative_path):目录段为 品名/品牌/机型/切片器,
        均保留空格/中文原样(validate 已拒分隔符与保留字符)。
        """
        return profile.repo_relative_path()

    # ------------------------------------------------------------------
    # 低层 HTTP
    # ------------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> Any:
        url = f"{self.api_base}{path}"
        headers = {
            "Authorization": f"token {self.access_token}",
            "Accept": "application/json",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload: Any = None
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:
                pass
            code = exc.code

            if code == 404 and allow_404:
                return None
            if code in (401, 403):
                remaining = exc.headers.get("X-RateLimit-Remaining")
                if remaining == "0":
                    raise RetryableError(
                        f"Git API 触发限流(HTTP 403),稍后自动重试"
                    ) from exc
                raise PermanentError(
                    f"Git API 鉴权失败(HTTP {code}): {payload or exc}"
                ) from exc
            if code in (429,) or 500 <= code < 600:
                raise RetryableError(
                    f"Git API 临时错误(HTTP {code}): {payload or exc}"
                ) from exc
            if code in (409, 422):
                raise _HttpError(code, payload) from exc
            raise GitRepositoryError(
                f"Git API 请求失败(HTTP {code}): {payload or exc}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise RetryableError(
                f"Git API 网络错误: {type(exc).__name__}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # 仓库元信息
    # ------------------------------------------------------------------
    def _repo_default_branch(self) -> str:
        if self._default_branch is None:
            meta = self._request("GET", f"/repos/{self._owner}/{self._repo}")
            default = meta.get("default_branch") if meta else None
            if not default:
                raise GitRepositoryError(
                    f"无法从仓库 API 获取 default_branch: {self._owner}/{self._repo}"
                )
            self._default_branch = str(default)
        return self._default_branch

    def _branch_sha(self, branch: str) -> str | None:
        data = self._request(
            "GET",
            f"/repos/{self._owner}/{self._repo}/branches/{branch}",
            allow_404=True,
        )
        if data is None:
            return None
        commit = data.get("commit") or {}
        # GitHub 的 branch commit 用 sha 字段;Gitea 用 id 字段
        sha = commit.get("sha") or commit.get("id")
        return str(sha) if sha else None

    # ------------------------------------------------------------------
    # P4/P5 公开操作(幂等组合见 submit_profile)
    # ------------------------------------------------------------------
    def find_submission(
        self,
        submission_id: str,
    ) -> PullRequestResult | None:
        """按确定性 branch 查找该提交已建好的 PR。

        覆盖场景:Git 成功但 Bitable 回写失败后的重试 ——
        此时 PR 已存在,直接返回,绝不新建第二个(P4 #21/#22)。
        """
        branch = self.branch_name_for(submission_id)
        pulls = self._list_pulls()
        for pull in pulls:
            # 只认 open 或已合并的 PR;已关闭未合并视为旧提交流程被手动终止,
            # 不应被静默复用(继续走新建分支逻辑会得到明确的冲突报错)。
            state = pull.get("state")
            if not (state == "open" or pull.get("merged") is True):
                continue
            head_ref = ((pull.get("head") or {}).get("ref")) or ""
            if head_ref == branch:
                url = pull.get("html_url") or pull.get("url")
                if not url:
                    continue
                return PullRequestResult(branch_name=branch, pull_request_url=url)
        return None

    def submission_pr_state(self, submission_id: str) -> str | None:
        """该提交的 PR 当前状态(供审查同步): "merged"/"open"/"closed"/None。

        已合并的 PR state 通常为 closed 且 merged=true,需先判 merged;
        None 表示该 branch 上没有任何 PR。
        """
        pull = self._find_pull_for_branch(submission_id)
        if pull is None:
            return None
        if pull.get("merged") is True:
            return "merged"
        state = pull.get("state")
        return "open" if state == "open" else "closed"

    def _find_pull_for_branch(
        self, submission_id: str
    ) -> dict[str, Any] | None:
        """该提交 branch 上第一个 PR 的原始记录(无则 None)。

        V2-P3:关闭理由读取与状态判定共用同一次 PR 列表扫描。
        """
        branch = self.branch_name_for(submission_id)
        for pull in self._list_pulls():
            head_ref = ((pull.get("head") or {}).get("ref")) or ""
            if head_ref == branch:
                return pull
        return None

    def pr_close_reason(self, submission_id: str) -> str | None:
        """V2-P3:该提交被关闭(未合并)PR 的关闭理由 = 关闭前最后一条评论正文。

        候选评论 = 普通评论(issues/{number}/comments,GitHub/Gitea 同构,
        系统事件不含在内)中 created_at 不晚于 PR closed_at 的正文非空评论;
        取时间最晚的一条。无候选评论返回 None;读取失败抛异常,
        由调用方记录日志(状态推进不受影响)。
        """
        pull = self._find_pull_for_branch(submission_id)
        if pull is None or pull.get("merged") is True:
            return None
        if pull.get("state") != "closed":
            return None
        number = pull.get("number")
        if number is None:
            return None
        closed_at = self._parse_rfc3339(pull.get("closed_at"))

        candidates: list[tuple[datetime | None, str]] = []
        for comment in self._list_issue_comments(int(number)):
            body = str(comment.get("body") or "").strip()
            if not body:
                continue
            created_at = self._parse_rfc3339(comment.get("created_at"))
            if (
                closed_at is not None
                and created_at is not None
                and created_at > closed_at
            ):
                continue  # 关闭之后的评论不是关闭理由
            candidates.append((created_at, body))
        if not candidates:
            return None
        # 时间最晚的一条;时间解析失败(罕见)的记录按最早兜底排序
        candidates.sort(key=lambda item: item[0] or datetime.min)
        return candidates[-1][1]

    def _list_issue_comments(self, number: int) -> list[dict[str, Any]]:
        """分页拉取该 PR(number)的普通评论(issue comments)。"""
        collected: list[dict[str, Any]] = []
        page = 1
        while True:
            query = urllib.parse.urlencode({"page": page, "per_page": 100})
            path = (
                f"/repos/{self._owner}/{self._repo}/issues/{number}/comments"
                f"?{query}"
            )
            data = self._request("GET", path)
            if not isinstance(data, list):
                raise GitRepositoryError(
                    f"评论列表响应格式异常: #{number}"
                )
            collected.extend(data)
            if len(data) < 100:
                break
            page += 1
        return collected

    @staticmethod
    def _parse_rfc3339(value: object) -> datetime | None:
        """解析 Git API 的时间戳(RFC3339,可能带 Z 或 ±HH:MM)。

        缺失/非法返回 None;naive 时间按 UTC 解释,保证可比。
        """
        if not value:
            return None
        text = str(value).strip()
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment

    def _list_pulls(self) -> list[dict[str, Any]]:
        """分页拉取本仓库全部 PR(state=all),返回原始 JSON 列表。"""
        collected: list[dict[str, Any]] = []
        page = 1
        while True:
            query = urllib.parse.urlencode(
                {"state": "all", "page": page, "per_page": 100}
            )
            path = (
                f"/repos/{self._owner}/{self._repo}/pulls?{query}"
            )
            data = self._request("GET", path)
            if not isinstance(data, list):
                raise GitRepositoryError(
                    f"PR 列表响应格式异常: {self._owner}/{self._repo}"
                )
            collected.extend(data)
            if len(data) < 100:
                break
            page += 1
        return collected

    def create_branch(self, branch_name: str) -> None:
        """从默认分支 HEAD 创建分支;已存在则直接返回(幂等)。"""
        if self._branch_sha(branch_name) is not None:
            return
        base_branch = self._repo_default_branch()
        base_sha = self._branch_sha(base_branch)
        if not base_sha:
            # 仓库元信息能取到 default_branch 但该分支无 commit =>
            # 典型原因是仓库还是空的(从未推送过任何提交),此时无法建分支。
            raise PermanentError(
                f"Git 仓库没有可用的基线提交:默认分支 {base_branch} "
                f"不存在 HEAD(仓库可能是空的)。请先在仓库推送一次初始提交"
                f"(如 README),再重新提交该材料"
            )
        try:
            if self._github:
                self._request(
                    "POST",
                    f"/repos/{self._owner}/{self._repo}/git/refs",
                    body={
                        "ref": f"refs/heads/{branch_name}",
                        "sha": base_sha,
                    },
                )
            else:
                self._request(
                    "POST",
                    f"/repos/{self._owner}/{self._repo}/branches",
                    body={
                        "new_branch_name": branch_name,
                        "old_branch_name": base_branch,
                    },
                )
        except _HttpError as exc:
            if exc.status in (409, 422):  # 并发下另一 worker 先建了
                return
            raise

    def write_profile(self, profile: MaterialProfile) -> str:
        """返回档案在仓库中的目标路径(纯函数,不触网)。"""
        return self.profile_path(profile)

    def commit(
        self,
        branch_name: str,
        message: str,
        path: str,
        content: str,
    ) -> str:
        """把文件写/更新到远端分支(Contents API 的一次 PUT 即 commit+push)。

        返回提交 SHA。
        """
        current = self._get_file(branch_name, path)
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch_name,
        }
        if current is not None:
            # 内容相同 -> 不产生空提交(保持幂等)
            if current["content"] == content:
                return current["sha"]
            body["sha"] = current["sha"]
        try:
            resp = self._request(
                "PUT",
                f"/repos/{self._owner}/{self._repo}/contents/{urllib.parse.quote(path, safe='/')}",
                body=body,
            )
        except _HttpError as exc:
            if exc.status == 422:  # sha 冲突/内容与远端并发修改
                raise RetryableError(
                    f"Git 文件更新冲突(HTTP 422),重试将重新读取远端: {exc}"
                ) from exc
            raise
        commit_info = resp.get("commit") if isinstance(resp, dict) else None
        # GitHub 返回 commit.sha;Gitea 部分响应只带 commit.id
        sha = commit_info.get("sha") if commit_info else None
        sha = sha or (commit_info.get("id") if commit_info else None)
        if not sha:
            raise GitRepositoryError(f"Contents API 未返回 commit sha: {path}")
        return str(sha)

    def _get_file(self, branch: str, path: str) -> dict[str, str] | None:
        """读取 branch 上的文件;不存在返回 None。

        返回 {sha, content(str 原文)};内容经 base64 解码并保持原文用于比较。
        """
        data = self._request(
            "GET",
            f"/repos/{self._owner}/{self._repo}/contents/"
            f"{urllib.parse.quote(path, safe='/')}?ref={urllib.parse.quote(branch)}",
            allow_404=True,
        )
        if data is None:
            return None
        try:
            raw = base64.b64decode(data["content"]).decode("utf-8")
        except (KeyError, ValueError) as exc:
            raise GitRepositoryError(
                f"远端文件内容无法解码: {self._owner}/{self._repo}/{path}"
            ) from exc
        return {"sha": str(data["sha"]), "content": raw}

    def push(self, branch_name: str) -> None:
        """HTTP API 模式下 commit 即已推送。

        本方法校验 commit 确实落在远端分支上:Content PUT 之后
        分支 head 应可读取;若远端迟迟不可见则视为瞬时错误可重试。
        """
        if self._branch_sha(branch_name) is None:
            raise RetryableError(
                f"推送校验失败:远端分支 {branch_name} 尚不可见"
            )

    def create_pull_request(
        self,
        branch_name: str,
        title: str,
        body: str,
    ) -> PullRequestResult:
        """为目标 branch 建 PR;该 head 已有 PR 时抛 _PullRequestExists。"""
        payload = {
            "title": title,
            "head": branch_name,
            "base": self._repo_default_branch(),
            "body": body,
        }
        try:
            resp = self._request(
                "POST",
                f"/repos/{self._owner}/{self._repo}/pulls",
                body=payload,
            )
        except _HttpError as exc:
            if exc.status in (409, 422):
                raise _PullRequestExists(exc.status, exc.payload) from exc
            raise
        url = resp.get("html_url") if isinstance(resp, dict) else None
        if not url:
            raise GitRepositoryError(f"创建 PR 后未拿到 html_url: {resp}")
        return PullRequestResult(
            branch_name=branch_name,
            pull_request_url=str(url),
        )

    # ------------------------------------------------------------------
    # 高层幂等编排(P4 #18-#22)
    # ------------------------------------------------------------------
    def submit_profile(
        self,
        submission_id: str,
        profile: MaterialProfile,
    ) -> PullRequestResult:
        """幂等地把一个提交的材料档案落库并开 PR。

        步骤:已有 PR -> 返回;无则 建 branch -> 写/更新档案文件
        (内容相同则跳过)-> 建 PR。任一步失败后整体重试都只会收敛
        到同一个 branch/同一个 PR,绝不会产生重复 PR。
        """
        existing = self.find_submission(submission_id)
        if existing is not None:
            return existing

        branch = self.branch_name_for(submission_id)
        self.create_branch(branch)

        path = self.profile_path(profile)
        content = profile.to_json(submission_id=submission_id)
        message = (
            f"[material] {profile.repo_name()} "
            f"(submission {submission_id[:8]})"
        )
        self.commit(branch, message, path, content)
        self.push(branch)

        # 内容与默认分支一致(如已合并过且无改动再次提交)-> 没有可提交的变更
        base_sha = self._branch_sha(self._repo_default_branch())
        head_sha = self._branch_sha(branch)
        if base_sha == head_sha:
            raise PermanentError(
                "材料内容与仓库默认分支一致,没有新的变更,未创建 PR"
            )

        title = f"[材料提交] {profile.repo_name()}"
        body = (
            f"材料: {profile.repo_name()}\n"
            f"品名: {profile.name}\n"
            f"机型: {profile.brand} {profile.model} ({profile.slicer})\n"
            f"喷嘴温度: {profile.nozzle_temperature} °C\n"
            f"最大体积流速: {profile.filament_max_volumetric_speed} mm³/s\n\n"
            f"Submission: {submission_id}\n"
        )
        try:
            return self.create_pull_request(branch, title, body)
        except _PullRequestExists:
            # 并发/上次建 PR 成功后未回写 —— 找到既有 PR 返回
            found = self.find_submission(submission_id)
            if found is None:
                raise PermanentError(
                    f"该提交(branch={branch})已存在一个未合并且已关闭的 PR:"
                    f"请关闭该旧分支后重试,或在表格中把状态置为 已拒绝/已通过"
                    f"后作为新一轮提交重新发起"
                ) from None
            return found
