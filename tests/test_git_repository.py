"""GitRepository 测试:用假 HTTP 服务器验证幂等与错误分类(无需真实仓库)。"""
import base64
import io
import json
import urllib.error
import urllib.parse
from urllib.request import Request

import pytest

from material_worker.adapters.git import GitRepository, PullRequestResult
from material_worker.domain.profile import MaterialProfile
from material_worker.exceptions import PermanentError, RetryableError


def make_profile():
    return MaterialProfile(
        id="Test PLA",
        name="Test PLA",
        nozzle_temperature=220,
        max_volumetric_speed=20,
    )


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
    """内存 Git 服务端:GitHub / Gitea 双形态,branch/contents/pulls 基本语义。"""

    def __init__(self, prefix: str):
        self.prefix = prefix  # /repos/o/r 或 /api/v1/repos/o/r
        self.heads = {"main": "c-main"}
        self.contents: dict[str, str] = {}
        self.file_shas: dict[str, str] = {}
        self.pulls: list[dict] = []
        self.pull_seq = 0
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
            return self._ok({"ref": body["ref"], "object": {"sha": body["sha"]}})

        if rest == "branches" and method == "POST":  # Gitea 建分支
            name = body["new_branch_name"]
            if name in self.heads:
                return self._error(url, 409, {"message": "branch exists"})
            self.heads[name] = self.heads[body.get("old_branch_name", "main")]
            return self._ok({"name": name, "commit": {"sha": self.heads[name]}})

        file_path_raw = self._rest_of("contents", rest)
        if file_path_raw is not None:
            file_path = urllib.parse.unquote(file_path_raw)
            ref = query.get("ref", ["main"])[0]
            if self._branch(ref) is None:
                return self._error(url, 422, {"message": "bad branch"})
            if method == "GET":
                content = self.contents.get(file_path)
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
                if new_content == self.contents.get(file_path):
                    # 内容未变:不推进分支 head(与真实语义一致地幂等)
                    return self._ok({"commit": {"sha": self.heads[branch]}})
                if file_path not in self.contents or body.get("sha"):
                    pass
                self.contents[file_path] = new_content
                self.file_shas[file_path] = self._next_sha()
                sha = self._next_sha()
                self.heads[branch] = sha
                return self._ok({"commit": {"sha": sha}})

        if rest == "pulls":
            if method == "GET":
                return self._ok(self.pulls)
            if method == "POST":  # 建 PR
                head = body["head"]
                if any(p["head"]["ref"] == head for p in self.pulls):
                    return self._error(
                        url, 422,
                        {"message": "A pull request already exists "
                                    "for these targets"},
                    )
                self.pull_seq += 1
                entry = {
                    "head": {"ref": head},
                    "html_url": f"https://host.example/pulls/{self.pull_seq}",
                    "number": self.pull_seq,
                    "state": "open",
                }
                self.pulls.append(entry)
                return self._ok(entry)

        return self._error(url, 404, {"message": f"unhandled {method} {rest}"})


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


def test_submit_profile_creates_pr(github_repo, github_server):
    result = github_repo.submit_profile("sid-1", make_profile())

    assert isinstance(result, PullRequestResult)
    assert result.branch_name == "material/sid-1"
    assert len(github_server.pulls) == 1

    stored = github_server.contents["materials/Test_PLA.json"]
    payload = json.loads(stored)
    assert payload["name"] == "Test PLA"
    assert payload["submission_id"] == "sid-1"


def test_submit_profile_idempotent_no_duplicate_pr(github_repo, github_server):
    first = github_repo.submit_profile("sid-1", make_profile())
    second = github_repo.submit_profile("sid-1", make_profile())

    assert second.pull_request_url == first.pull_request_url
    assert len(github_server.pulls) == 1

    # 只发生过一次文件写入(第二次全部命中幂等路径)
    puts = [m for m in github_server.log if m[0] == "PUT"]
    assert len(puts) == 1


def test_partial_success_recovers_without_duplicate_content_commit(
    github_repo, github_server
):
    """分支/文件已就位但 PR 创建失败 -> 重试收敛到同一 PR,内容不重复提交。"""
    github_server.force_status = 500
    with pytest.raises(RetryableError):
        github_repo.submit_profile("sid-1", make_profile())
    assert len(github_server.pulls) == 0  # PR 没建成

    github_server.force_status = None
    result = github_repo.submit_profile("sid-1", make_profile())

    assert len(github_server.pulls) == 1
    puts = [m for m in github_server.log if m[0] == "PUT"]
    assert len(puts) == 1  # 内容未变,重试不重复提交文件


def test_no_changes_raises_permanent(github_repo, github_server):
    """内容与默认分支一致(例如已合并后无改动再次提交)-> 不建空 PR。"""
    profile = make_profile()
    github_server.contents["materials/Test_PLA.json"] = profile.to_json(
        submission_id="sid-1"
    )
    github_server.file_shas["materials/Test_PLA.json"] = "sha-existing"

    with pytest.raises(PermanentError):
        github_repo.submit_profile("sid-1", profile)
    assert github_server.pulls == []


def test_auth_error_is_permanent(github_repo, github_server):
    github_server.force_status = 401
    with pytest.raises(PermanentError):
        github_repo.submit_profile("sid-1", make_profile())


def test_transient_5xx_is_retryable(github_repo, github_server):
    github_server.force_status = 503
    with pytest.raises(RetryableError):
        github_repo.submit_profile("sid-1", make_profile())


def test_gitea_create_branch_uses_gitea_endpoint(gitea_repo):
    repo, server = gitea_repo
    result = repo.submit_profile("sid-9", make_profile())

    assert len(server.pulls) == 1
    branch_posts = [
        (m, u, b)
        for m, u, b in server.log
        if m == "POST" and u.endswith("/branches")
    ]
    assert len(branch_posts) == 1
    _, url, body = branch_posts[0]
    assert "api/v1" in url
    assert body["new_branch_name"] == "material/sid-9"
    assert result.branch_name == "material/sid-9"


def _add_pull(server, submission_id, state="open", merged=False):
    server.pulls.append(
        {
            "head": {"ref": f"material/{submission_id}"},
            "html_url": f"https://host.example/pulls/x-{submission_id}",
            "number": len(server.pulls) + 1,
            "state": state,
            **({"merged": True} if merged else {}),
        }
    )


def test_submission_pr_state_open(github_repo, github_server):
    _add_pull(github_server, "sid-1", state="open")
    assert github_repo.submission_pr_state("sid-1") == "open"


def test_submission_pr_state_merged(github_repo, github_server):
    # 已合并的 PR:state=closed + merged=true(须判为 merged 而非 closed)
    _add_pull(github_server, "sid-1", state="closed", merged=True)
    assert github_repo.submission_pr_state("sid-1") == "merged"


def test_submission_pr_state_closed_unmerged(github_repo, github_server):
    _add_pull(github_server, "sid-1", state="closed")
    assert github_repo.submission_pr_state("sid-1") == "closed"


def test_submission_pr_state_ignores_other_branches(github_repo, github_server):
    _add_pull(github_server, "other", state="merged", merged=True)
    _add_pull(github_server, "sid-1", state="open")
    assert github_repo.submission_pr_state("sid-1") == "open"


def test_submission_pr_state_none_when_no_pr(github_repo, github_server):
    _add_pull(github_server, "other", state="open")
    assert github_repo.submission_pr_state("sid-missing") is None


def test_repository_url_parsing():
    gh = GitRepository("https://github.com/o/r.git", "t")
    assert gh.api_base == "https://api.github.com"
    assert gh._owner == "o"
    assert gh._repo == "r"

    with pytest.raises(PermanentError):
        GitRepository("git@github.com:o/r.git", "t")
    with pytest.raises(PermanentError):
        GitRepository("https://github.com/only-owner", "t")
