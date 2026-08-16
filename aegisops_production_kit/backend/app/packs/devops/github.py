"""devops.github pack (P4). Read tools (repo existence, Actions run status) for the harness.
Repository/CI MUTATION (ensure_repo/upsert_file/PR/dispatch) is DECLARED as propose specs
and stays the governed path (PR-first; direct default-branch pushes remain banned)."""

from __future__ import annotations

from ...settings import Settings
from ...tools.github import get_github
from ..base import CapabilityPack, ToolSpec


def build(settings: Settings) -> CapabilityPack:
    gh = get_github(settings)

    async def repo_exists(repo: str):
        return await gh.repo_exists(repo)

    async def get_run(repo: str, run_id: int):
        return await gh.get_run(repo, run_id)

    return CapabilityPack(
        name="devops.github", provider="github", domain="devops",
        tools=(
            ToolSpec("devops.github.repo_exists", "Check whether a repository exists", "repo", "read", repo_exists),
            ToolSpec("devops.github.get_run", "Get an Actions workflow run's status", "ci", "read", get_run),
            # Declared change capabilities — PR-first governed flow only (never executed here).
            ToolSpec("devops.github.open_pr", "Open a pull request (governed change flow)", "repo",
                     "propose"),
            ToolSpec("devops.github.dispatch_workflow", "Dispatch a CI workflow (governed)", "ci",
                     "propose"),
        ),
        knowledge=("Changes land via pull requests; direct pushes to the default branch are "
                   "banned by policy. CI diagnosis reads run status + logs.",),
        enabled=lambda s: bool(getattr(get_github(s), "enabled", True)),
    )
