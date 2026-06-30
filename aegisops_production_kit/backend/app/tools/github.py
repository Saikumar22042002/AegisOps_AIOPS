"""Real GitHub client (PyGithub) for the DevOps agent.

Repos, branches, file commits, pull requests, Actions workflow dispatch + run tracking,
and GitHub Actions Secrets. PyGithub is synchronous, so calls run in a worker thread.
Side-effecting operations (commit/PR/dispatch/deploy) are only invoked after the approval
gate in the graph; this client itself does not bypass it.
"""

from __future__ import annotations

import base64
from typing import Any

import anyio
import structlog

from ..settings import Settings

log = structlog.get_logger(__name__)

try:
    from github import Auth, Github, GithubException

    _HAVE_GITHUB = True
except Exception:  # noqa: BLE001
    _HAVE_GITHUB = False


class GitHubError(Exception):
    pass


class GitHubClient:
    def __init__(self, settings: Settings) -> None:
        self.token = settings.github_token
        self.org = settings.github_org
        self.enabled = bool(_HAVE_GITHUB and self.token)
        self._gh: Any = None
        if self.enabled:
            self._gh = Github(auth=Auth.Token(self.token), per_page=100)

    def _require(self) -> None:
        if not self.enabled:
            raise GitHubError("GITHUB_TOKEN is not configured")

    def _full_name(self, repo: str) -> str:
        return repo if "/" in repo else f"{self.org}/{repo}"

    async def _run(self, fn, *args, **kwargs):
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))

    # ── reads ──
    async def get_repo(self, repo: str) -> Any:
        self._require()
        return await self._run(self._gh.get_repo, self._full_name(repo))

    async def repo_exists(self, repo: str) -> bool:
        self._require()
        try:
            await self.get_repo(repo)
            return True
        except GithubException:
            return False

    async def ensure_repo(self, repo: str, private: bool = True, description: str = "") -> Any:
        """Create the repo if absent (in the configured org), else return it."""
        self._require()
        if await self.repo_exists(repo):
            return await self.get_repo(repo)
        name = repo.split("/")[-1]
        if self.org:
            org = await self._run(self._gh.get_organization, self.org)
            return await self._run(org.create_repo, name, private=private, description=description, auto_init=True)
        user = await self._run(self._gh.get_user)
        return await self._run(user.create_repo, name, private=private, description=description, auto_init=True)

    # ── writes (post-approval) ──
    async def upsert_file(self, repo: str, path: str, content: str, message: str, branch: str = "main") -> dict[str, Any]:
        self._require()
        r = await self.get_repo(repo)
        try:
            existing = await self._run(r.get_contents, path, ref=branch)
            res = await self._run(r.update_file, path, message, content, existing.sha, branch=branch)
        except GithubException:
            res = await self._run(r.create_file, path, message, content, branch=branch)
        commit = res["commit"]
        return {"sha": commit.sha, "path": path, "branch": branch}

    async def create_pull_request(self, repo: str, title: str, head: str, base: str = "main", body: str = "") -> dict[str, Any]:
        self._require()
        r = await self.get_repo(repo)
        pr = await self._run(r.create_pull, title=title, body=body, head=head, base=base)
        return {"number": pr.number, "url": pr.html_url, "state": pr.state}

    async def dispatch_workflow(self, repo: str, workflow_file: str, ref: str = "main", inputs: dict | None = None) -> bool:
        self._require()
        r = await self.get_repo(repo)
        wf = await self._run(r.get_workflow, workflow_file)
        return await self._run(wf.create_dispatch, ref, inputs or {})

    async def latest_run_status(self, repo: str, workflow_file: str) -> dict[str, Any]:
        self._require()
        r = await self.get_repo(repo)
        wf = await self._run(r.get_workflow, workflow_file)
        runs = await self._run(lambda: list(wf.get_runs()[:1]))
        if not runs:
            return {"status": "none"}
        run = runs[0]
        return {"id": run.id, "status": run.status, "conclusion": run.conclusion, "url": run.html_url}

    async def set_actions_secret(self, repo: str, name: str, value: str) -> bool:
        """Store an encrypted GitHub Actions secret (PyGithub seals it with the repo key)."""
        self._require()
        r = await self.get_repo(repo)
        await self._run(r.create_secret, name, value, "actions")
        log.info("github.secret_set", repo=self._full_name(repo), name=name)
        return True

    async def ping(self) -> bool:
        self._require()
        user = await self._run(self._gh.get_user)
        return await self._run(lambda: user.login) is not None

    @staticmethod
    def b64(content: str) -> str:
        return base64.b64encode(content.encode()).decode()


_client: GitHubClient | None = None


def get_github(settings: Settings) -> GitHubClient:
    global _client
    if _client is None:
        _client = GitHubClient(settings)
    return _client
