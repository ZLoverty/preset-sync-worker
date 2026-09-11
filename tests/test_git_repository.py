"""GitRepository 测试:用假 HTTP 服务器验证幂等与错误分类(无需真实仓库)。

V3 变化:
- `submit_profile(profile, …)`:身份/切片软件决定 branch,档案文件里
  只有材料数据(无溯源键);
- 审查同步按 `PR URL` 反查 PR(GET /pulls/{number}),不再按 branch 列表扫描;
- 空变更保护按**内容**比对默认分支,不建空 PR。
"""
import base64
import io
import json
import urllib.error
import urllib.parse
from urllib.request import Request

import pytest

from helpers import (
    DEFAULT_BRANCH,
    DEFAULT_REPO_PATH,
    DEFAULT_SUBMITTER,
    DEFAULT_SUBMIT_TIME,
    make_profile as make_valid_profile,
)

from material_worker import fields
from material_worker.adapters.git import GitRepository, PullRequestResult
from material_worker.exceptions import PermanentError, RetryableError

# V3-P4:默认档案(L1002 / BBL P2S / BambuStudio)的仓库内路径
REPO_PATH = DEFAULT_REPO_PATH


def make_profile():
    return make_valid_profile()


class _Response:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeGitServer:
    """内存 Git 服务端:GitHub / Gitea 双形态,branch/contents/pulls 基本语义。

    文件按 **branch** 分桶(建分支时从基线复制)—— 这样才能真实区分
    「默认分支的旧内容」与「本轮分支写入的新内容」(空变更保护依赖它)。
    """

    def __init__(self, prefix: str):
        self.prefix = prefix  # /repos/o/r 或 /api/v1/repos/o/r
        self.heads = {"main": "c-main"}
        self.files: dict[str, dict[str, str]] = {"main": {}}
        self.file_shas: dict[str, str] = {}
        self.pulls: list[dict] = []
        self.pull_seq = 0
        # V2-P3:PR(issue)普通评论 -> {pull number: [comment dict]}
        self.issue_comments: dict[int, list[dict]] = {}
        self._counter = 0
        self.log: list[tuple[str, str, dict | None]] = []
        self.force_status: int | None = None

    def _next_sha(self) -> str:
        self._counter += 1
        return f"c{self._counter:04d}"

    def __call__(self, req: Request, timeout: float | None = None):
        method = req.get_method()
        url = req.full_url
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query)
        body = json.loads(req.data) if req.data else None
        self.log.append((method, url, body))

        if self.force_status:
            return self._error(url, self.force_status, {"message": "forced"})

        rest = parsed.path[len(self.prefix):].strip("/")
        return self._route(method, rest, query, body, url)

    # -- helpers -------------------------------------------------------
    def _ok(self, payload: dict | list) -> _Response:
        return _Response(json.dumps(payload).encode("utf-8"))

    def _error(self, url: str, code: int, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        return _raise_http(url, code, io.BytesIO(data))

    def _branch(self, name: str) -> str | None:
        return self.heads.get(name)

    def _rest_of(self, kind: str, rest: str) -> str | None:
        if not rest.startswith(f"{kind}/"):
            return None
        return rest[len(kind) + 1:]

    def _files(self, branch: str) -> dict[str, str]:
        return self.files.setdefault(branch, {})

    def _copy_branch(self, new: str, source_sha: str) -> None:
        """建分支 = 从某个 commit 分叉:复制该 commit 所属分支的文件树。"""
        origin = next(
            (b for b, sha in self.heads.items() if sha == source_sha), "main"
        )
        self.files[new] = dict(self._files(origin))

    def seed_file(self, content: str, *, branch: str = "main", path: str = REPO_PATH):
        """在某个分支上预置文件内容(用于构造「已存在/无变更」场景)。"""
        self._files(branch)[path] = content
        self.file_shas[path] = self._next_sha()

    # -- routing -------------------------------------------------------
    def _route(self, method, rest, query, body, url):
        if rest == "" and method == "GET":  # 仓库元信息
            return self._ok({"default_branch": "main"})

        branch = self._rest_of("branches", rest)
        if branch is not None and method == "GET":
            sha = self._branch(branch)
            if sha is None:
                return self._error(url, 404, {"message": "branch not found"})
            # 真实差异:GitHub 的 branch commit 用 sha 字段,Gitea 用 id。
            # Gitea 前缀(/api/v1)按真实形态只返回 id,回归锁 sha/id 兼容。
            key = "id" if "api/v1" in self.prefix else "sha"
            return self._ok({"commit": {key: sha}})

        if rest.startswith("git/refs") and method == "POST":  # GitHub 建分支
            ref = body["ref"].removeprefix("refs/heads/")
            if ref in self.heads:
                return self._error(url, 422, {"message": "ref already exists"})
            self.heads[ref] = body["sha"]
            self._copy_branch(ref, body["sha"])
            return self._ok({"ref": body["ref"], "object": {"sha": body["sha"]}})

        if rest == "branches" and method == "POST":  # Gitea 建分支
            name = body["new_branch_name"]
            if name in self.heads:
                return self._error(url, 409, {"message": "branch exists"})
            self.heads[name] = self.heads[body.get("old_branch_name", "main")]
            self._copy_branch(name, self.heads[name])
            return self._ok({"name": name, "commit": {"sha": self.heads[name]}})

        file_path_raw = self._rest_of("contents", rest)
        if file_path_raw is not None:
            file_path = urllib.parse.unquote(file_path_raw)
            ref = query.get("ref", ["main"])[0]
            if self._branch(ref) is None:
                return self._error(url, 422, {"message": "bad branch"})
            if method == "GET":
                content = self._files(ref).get(file_path)
                if content is None:
                    return self._error(url, 404, {"message": "file not found"})
                payload = {
                    "sha": self.file_shas[file_path],
                    "encoding": "base64",
                    "content": base64.b64encode(content.encode("utf-8"))
                    .decode("ascii"),
                }
                return self._ok(payload)
            if method == "PUT":
                new_content = base64.b64decode(body["content"]).decode("utf-8")
                branch = body["branch"]
                store = self._files(branch)
                if new_content == store.get(file_path):
                    # 内容未变:不推进分支 head(与真实语义一致地幂等)
                    return self._ok({"commit": {"sha": self.heads[branch]}})
                store[file_path] = new_content
                self.file_shas[file_path] = self._next_sha()
                sha = self._next_sha()
                self.heads[branch] = sha
                return self._ok({"commit": {"sha": sha}})

        if rest == "pulls":
            if method == "GET":
                return self._ok(self.pulls)
            if method == "POST":  # 建 PR
                head = body["head"]
                # 真实语义:同一 head 上已有**打开**的 PR 才会 422
                if any(
                    p["head"]["ref"] == head and p.get("state") == "open"
                    for p in self.pulls
                ):
                    return self._error(
                        url, 422,
                        {"message": "A pull request already exists "
                                    "for these targets"},
                    )
                self.pull_seq += 1
                entry = {
                    "head": {"ref": head},
                    "html_url": self._pull_url(self.pull_seq),
                    "number": self.pull_seq,
                    "state": "open",
                }
                self.pulls.append(entry)
                return self._ok(entry)

        pull_number = self._rest_of("pulls", rest)
        if pull_number is not None and method == "GET":
            for pull in self.pulls:
                if str(pull["number"]) == pull_number:
                    return self._ok(pull)
            return self._error(url, 404, {"message": "pull not found"})

        # V2-P3:PR 的普通评论(GitHub/Gitea 同构 /issues/{number}/comments)
        if method == "GET" and rest.startswith("issues/") and rest.endswith(
            "/comments"
        ):
            number = int(rest[len("issues/"):-len("/comments")])
            return self._ok(self.issue_comments.get(number, []))

        return self._error(url, 404, {"message": f"unhandled {method} {rest}"})

    def _pull_url(self, number: int) -> str:
        """GitHub 是 /pull/123,Gitea 是 /pulls/123 —— 两种形态都要能解析。"""
        kind = "pulls" if "api/v1" in self.prefix else "pull"
        return f"https://host.example/o/r/{kind}/{number}"

    def add_pull(
        self,
        *,
        branch: str = DEFAULT_BRANCH,
        state: str = "open",
        merged: bool = False,
        closed_at: str | None = None,
    ) -> str:
        """登记一个 PR(默认打开),返回其 URL。"""
        self.pull_seq += 1
        entry: dict = {
            "number": self.pull_seq,
            "head": {"ref": branch},
            "html_url": self._pull_url(self.pull_seq),
            "state": state,
        }
        if merged:
            entry["merged"] = True
        if closed_at is not None:  # V2-P3:关闭理由需与 closed_at 比对
            entry["closed_at"] = closed_at
        self.pulls.append(entry)
        return entry["html_url"]


def _raise_http(url: str, code: int, fp: io.BytesIO):
    raise urllib.error.HTTPError(url, code, "http error", {}, fp)


@pytest.fixture
def github_server():
    return FakeGitServer("/repos/o/r")


@pytest.fixture
def github_repo(monkeypatch, github_server):
    repo = GitRepository("https://github.com/o/r.git", "tok")
    monkeypatch.setattr(
        "urllib.request.urlopen", github_server
    )
    return repo


@pytest.fixture
def gitea_repo(monkeypatch):
    server = FakeGitServer("/api/v1/repos/o/r")
    repo = GitRepository("https://git.example.com/o/r.git", "tok")
    monkeypatch.setattr("urllib.request.urlopen", server)
    return repo, server


# ----------------------------------------------------------------------
# 提交:branch 派生 + 幂等 + 空变更保护
# ----------------------------------------------------------------------
def test_submit_profile_creates_pr(github_repo, github_server):
    result = github_repo.submit_profile(make_profile())

    assert isinstance(result, PullRequestResult)
    assert result.branch_name == DEFAULT_BRANCH
    assert len(github_server.pulls) == 1

    stored = github_server.files[DEFAULT_BRANCH][REPO_PATH]
    payload = json.loads(stored)
    # V3:键为身份四要素 + 结构 + 参数 —— 没有任何 worker 溯源键
    assert payload["name"] == "L1002@BBL P2S"
    assert payload["pi_code"] == "L1002"
    assert payload["printer"] == "BBL P2S"
    assert payload["slicer"] == "BambuStudio"
    assert payload["inherits"] == "Panchroma PLA"
    for key in ("submission", "submission_id", "generated_at", "submitted_at"):
        assert key not in payload
    assert "id" not in payload and "filament_settings_id" not in payload


def test_submit_profile_idempotent_no_duplicate_pr(github_repo, github_server):
    first = github_repo.submit_profile(make_profile())
    second = github_repo.submit_profile(make_profile())

    assert second.pull_request_url == first.pull_request_url
    assert len(github_server.pulls) == 1

    # 只发生过一次文件写入(第二次全部命中幂等路径)
    puts = [m for m in github_server.log if m[0] == "PUT"]
    assert len(puts) == 1


def test_submit_profile_reuses_current_round_pr_url(github_repo, github_server):
    """上一轮 PR 仍在打开(回写失败后重试)-> 直接收敛到同一 PR。"""
    url = github_server.add_pull()
    result = github_repo.submit_profile(make_profile(), url)

    assert result.pull_request_url == url
    assert len(github_server.pulls) == 1


def test_submit_profile_opens_new_round_after_previous_pr_closed(
    github_repo, github_server
):
    """上一轮 PR 已关闭 -> 再次提交开启新一轮(不复活旧 PR)。"""
    closed = github_server.add_pull(
        state="closed", closed_at="2026-09-01T08:00:00Z"
    )
    result = github_repo.submit_profile(make_profile(), closed)

    assert result.pull_request_url != closed
    assert len(github_server.pulls) == 2


def test_partial_success_recovers_without_duplicate_content_commit(
    github_repo, github_server
):
    """分支/文件已就位但 PR 创建失败 -> 重试收敛到同一 PR,内容不重复提交。"""
    github_server.force_status = 500
    with pytest.raises(RetryableError):
        github_repo.submit_profile(make_profile())
    assert len(github_server.pulls) == 0  # PR 没建成

    github_server.force_status = None
    github_repo.submit_profile(make_profile())

    assert len(github_server.pulls) == 1
    puts = [m for m in github_server.log if m[0] == "PUT"]
    assert len(puts) == 1  # 内容未变,重试不重复提交文件


def test_no_changes_raises_permanent(github_repo, github_server):
    """内容与默认分支一致(例如已合并后无改动再次提交)-> 不建空 PR。"""
    profile = make_profile()
    github_server.seed_file(profile.to_json())

    with pytest.raises(PermanentError):
        github_repo.submit_profile(profile)
    assert github_server.pulls == []


def test_auth_error_is_permanent(github_repo, github_server):
    github_server.force_status = 401
    with pytest.raises(PermanentError):
        github_repo.submit_profile(make_profile())


def test_transient_5xx_is_retryable(github_repo, github_server):
    github_server.force_status = 503
    with pytest.raises(RetryableError):
        github_repo.submit_profile(make_profile())


def test_gitea_create_branch_uses_gitea_endpoint(gitea_repo):
    repo, server = gitea_repo
    result = repo.submit_profile(make_profile())

    assert len(server.pulls) == 1
    branch_posts = [
        (m, u, b)
        for m, u, b in server.log
        if m == "POST" and u.endswith("/branches")
    ]
    assert len(branch_posts) == 1
    _, url, body = branch_posts[0]
    assert "api/v1" in url
    assert body["new_branch_name"] == DEFAULT_BRANCH
    assert result.branch_name == DEFAULT_BRANCH
    # Gitea 的 branch commit 只带 id 字段(/api/v1 前缀),仍能读到 head
    assert server.heads[DEFAULT_BRANCH] != "c-main"


# ----------------------------------------------------------------------
# V3-P7/P8:commit message 与 PR 正文
# ----------------------------------------------------------------------
def test_commit_message_carries_identity_submitter_and_records():
    message = GitRepository.commit_message(
        make_profile(),
        DEFAULT_SUBMITTER,
        DEFAULT_SUBMIT_TIME,
        ["调参记录.md", "曲线.png"],
    )
    assert message.splitlines()[0] == "[材料] L1002@BBL P2S (BambuStudio)"
    assert DEFAULT_SUBMITTER in message
    assert DEFAULT_SUBMIT_TIME in message
    assert "- 调参记录.md" in message and "- 曲线.png" in message


def test_commit_message_without_records_omits_section():
    message = GitRepository.commit_message(make_profile(), "张三", "2026-09-11")
    assert "过程记录" not in message
    assert message.splitlines()[0].startswith("[材料] L1002@BBL P2S")


def test_submit_profile_writes_records_into_commit_message(
    github_repo, github_server
):
    """附件本体不进 Git:仓库里只有档案文件,记录名只出现在 commit message。"""
    github_repo.submit_profile(
        make_profile(),
        submitter=DEFAULT_SUBMITTER,
        submit_time=DEFAULT_SUBMIT_TIME,
        process_records=["调参记录.md"],
    )

    puts = [m for m in github_server.log if m[0] == "PUT"]
    message = puts[0][2]["message"]
    assert "[材料] L1002@BBL P2S (BambuStudio)" in message
    assert "调参记录.md" in message
    # 仓库里没有多出任何附件文件
    assert set(github_server.files[DEFAULT_BRANCH]) == {REPO_PATH}


def _pull_request_body(github_server) -> str:
    posts = [m for m in github_server.log if m[0] == "POST" and m[1].endswith("/pulls")]
    assert len(posts) == 1
    return posts[0][2]["body"]


def test_pr_body_is_identity_then_diff_then_submitter(github_repo, github_server):
    """PR 正文 = 首行身份 + 人话版字段差异 + 提交人/时间(用户确认的形态)。"""
    github_repo.submit_profile(
        make_profile(),
        submitter=DEFAULT_SUBMITTER,
        submit_time=DEFAULT_SUBMIT_TIME,
    )
    posts = [m for m in github_server.log if m[0] == "POST" and m[1].endswith("/pulls")]
    assert posts[0][2]["title"] == "[材料提交] L1002@BBL P2S (BambuStudio)"

    lines = _pull_request_body(github_server).splitlines()
    assert lines[0] == "L1002@BBL P2S · BambuStudio"  # 第一行:简洁身份
    assert lines[-1] == (  # 最后一行:提交人与时间
        f"提交人: {DEFAULT_SUBMITTER} · 提交时间: {DEFAULT_SUBMIT_TIME}"
    )

    # 中间是差异:首次提交(默认分支上还没有该档案)-> 列出取值,无箭头
    assert f"{fields.INHERITS}: Panchroma PLA" in lines
    assert f"{fields.NOZZLE_TEMP}: 220 °C" in lines
    assert f"{fields.MAX_VOL_SPEED}: 18 mm³/s" in lines
    assert not [line for line in lines if "→" in line]

    # 内部键名/身份重复信息不进正文(审查者看的是表格上的说法)
    assert "nozzle_temperature" not in "\n".join(lines)
    assert "PI Code" not in "\n".join(lines)


def test_pr_body_shows_field_diff_against_default_branch(
    github_repo, github_server
):
    """同一身份的第二次提交:正文讲清「哪个字段从什么改成了什么」。"""
    github_server.seed_file(make_profile().to_json())
    github_repo.submit_profile(
        make_valid_profile(nozzle_temperature=215, filament_flow_ratio=0.92),
        submitter=DEFAULT_SUBMITTER,
        submit_time=DEFAULT_SUBMIT_TIME,
    )
    body = _pull_request_body(github_server)
    assert f"{fields.NOZZLE_TEMP}: 220 °C → 215 °C" in body
    assert f"{fields.FLOW_RATIO}: 0.95 → 0.92" in body
    assert fields.MAX_VOL_SPEED not in body  # 没动的字段不啰嗦


# ----------------------------------------------------------------------
# 审查同步:按 PR URL 反查(表里已无提交 ID)
# ----------------------------------------------------------------------
def test_pull_number_parses_both_providers():
    assert GitRepository.pull_number("https://github.com/o/r/pull/123") == 123
    assert GitRepository.pull_number("https://g.example/o/r/pulls/45") == 45
    assert GitRepository.pull_number("https://g.example/o/r/pulls/45?x=1") == 45
    assert GitRepository.pull_number("") is None
    assert GitRepository.pull_number("https://g.example/o/r/pulls") is None


def test_pr_state_open(github_repo, github_server):
    url = github_server.add_pull(state="open")
    assert github_repo.pr_state(url) == "open"


def test_pr_state_merged(github_repo, github_server):
    # 已合并的 PR:state=closed + merged=true(须判为 merged 而非 closed)
    url = github_server.add_pull(state="closed", merged=True)
    assert github_repo.pr_state(url) == "merged"


def test_pr_state_closed_unmerged(github_repo, github_server):
    url = github_server.add_pull(state="closed")
    assert github_repo.pr_state(url) == "closed"


def test_pr_state_none_for_empty_or_unknown_url(github_repo, github_server):
    assert github_repo.pr_state("") is None
    assert github_repo.pr_state("https://host.example/o/r/pull/999") is None
    assert github_repo.pr_state("not-a-url") is None


def test_pr_state_keyed_by_url_not_branch(github_repo, github_server):
    """同一 branch 上多轮 PR:按 URL 认,不会被别的轮次串扰。"""
    first = github_server.add_pull(state="closed", merged=True)
    second = github_server.add_pull(state="open")

    assert github_repo.pr_state(first) == "merged"
    assert github_repo.pr_state(second) == "open"


def test_find_open_pr_for_branch_ignores_closed(github_repo, github_server):
    github_server.add_pull(state="closed", merged=True)
    assert github_repo.find_open_pr_for_branch(DEFAULT_BRANCH) is None

    url = github_server.add_pull(state="open")
    found = github_repo.find_open_pr_for_branch(DEFAULT_BRANCH)
    assert found is not None and found.pull_request_url == url


def test_find_open_pr_for_branch_ignores_other_branches(github_repo, github_server):
    github_server.add_pull(branch="material/other", state="open")
    assert github_repo.find_open_pr_for_branch(DEFAULT_BRANCH) is None


# ----------------------------------------------------------------------
# V2-P3:pr_close_reason(关闭理由 = 关闭前最后一条非空普通评论)
# ----------------------------------------------------------------------
def test_pr_close_reason_last_comment_before_close(github_repo, github_server):
    url = github_server.add_pull(
        state="closed", closed_at="2026-09-02T08:00:00Z"
    )
    github_server.issue_comments[1] = [
        {"created_at": "2026-09-01T09:00:00Z", "body": "第一版问题太多"},
        {"created_at": "2026-09-01T10:00:00Z", "body": "需要补充测试数据"},
    ]
    assert github_repo.pr_close_reason(url) == "需要补充测试数据"


def test_pr_close_reason_skips_empty_body_and_comments_after_close(
    github_repo, github_server
):
    url = github_server.add_pull(
        state="closed", closed_at="2026-09-02T08:00:00Z"
    )
    github_server.issue_comments[1] = [
        {"created_at": "2026-09-02T09:00:00Z", "body": "关闭后的评论不算理由"},
        {"created_at": "2026-09-01T10:00:00Z", "body": "   "},
        {"created_at": "2026-09-01T11:00:00Z", "body": "缺材料 ID"},
    ]
    assert github_repo.pr_close_reason(url) == "缺材料 ID"


def test_pr_close_reason_compares_absolute_time_across_timezones(
    github_repo, github_server
):
    """created_at 带不同时区偏移,排序须按绝对时刻而非字符串。"""
    url = github_server.add_pull(
        state="closed", closed_at="2026-09-02T00:00:00Z"
    )
    github_server.issue_comments[1] = [
        # +08:00 的 07:59 = Z 的 前一日 23:59,仍早于关闭时刻
        {"created_at": "2026-09-02T07:59:00+08:00", "body": "时区偏移的评论"},
        {"created_at": "2026-09-01T23:30:00Z", "body": "Z 时区评论"},
    ]
    assert github_repo.pr_close_reason(url) == "时区偏移的评论"


def test_pr_close_reason_none_when_no_comments(github_repo, github_server):
    url = github_server.add_pull(
        state="closed", closed_at="2026-09-02T08:00:00Z"
    )
    assert github_repo.pr_close_reason(url) is None


def test_pr_close_reason_none_for_open_pr(github_repo, github_server):
    url = github_server.add_pull(state="open")
    assert github_repo.pr_close_reason(url) is None


def test_pr_close_reason_none_for_merged_pr(github_repo, github_server):
    # 已合并(merged=true)不是关闭未合并:不写理由
    url = github_server.add_pull(
        state="closed", merged=True, closed_at="2026-09-02T08:00:00Z"
    )
    github_server.issue_comments[1] = [
        {"created_at": "2026-09-01T10:00:00Z", "body": "合并不需要理由"},
    ]
    assert github_repo.pr_close_reason(url) is None


def test_pr_close_reason_none_for_empty_url(github_repo, github_server):
    assert github_repo.pr_close_reason("") is None


# ----------------------------------------------------------------------
# URL / 路径
# ----------------------------------------------------------------------
def test_profile_path_uses_v3_layout(github_repo):
    assert github_repo.profile_path(make_profile()) == REPO_PATH


def test_branch_name_comes_from_identity(github_repo):
    assert github_repo.branch_name_for("L1002@BBL P2S", "BambuStudio") == (
        DEFAULT_BRANCH
    )


def test_repository_url_parsing():
    gh = GitRepository("https://github.com/o/r.git", "t")
    assert gh.api_base == "https://api.github.com"
    assert gh._owner == "o"
    assert gh._repo == "r"

    with pytest.raises(PermanentError):
        GitRepository("git@github.com:o/r.git", "t")
    with pytest.raises(PermanentError):
        GitRepository("https://github.com/only-owner", "t")
