"""P16 — DevOps CI poll-to-completion.

Dispatching a workflow returns no run id, so the agent identifies the run it created and polls
THAT run to completion — reporting the real conclusion, never the latest-run guess and never a
faked success.
"""

from __future__ import annotations

import anyio
import pytest

from app.agents import devops as devops_mod
from app.tools.github import GitHubClient
from app.settings import get_settings


# ── poll_run_to_completion loops until the run is 'completed' ──────────────────────────────

async def test_poll_loops_until_completed(monkeypatch):
    settings = get_settings()
    gh = GitHubClient(settings)
    gh.enabled = True  # bypass the token requirement for this pure-logic test

    states = [
        {"id": 7, "status": "queued", "conclusion": None, "url": "u"},
        {"id": 7, "status": "in_progress", "conclusion": None, "url": "u"},
        {"id": 7, "status": "completed", "conclusion": "success", "url": "u"},
    ]
    seen = {"n": 0}

    async def _get_run(repo, run_id):
        i = min(seen["n"], len(states) - 1)
        seen["n"] += 1
        return states[i]

    monkeypatch.setattr(gh, "get_run", _get_run)
    monkeypatch.setattr(anyio, "sleep", lambda *_a, **_k: anyio.lowlevel.checkpoint())

    progress = []

    async def _on_poll(info):
        progress.append(info["status"])

    final = await gh.poll_run_to_completion("o/r", 7, interval=0.0, on_poll=_on_poll)
    assert final["status"] == "completed" and final["conclusion"] == "success"
    assert seen["n"] == 3                      # polled until completed, not once
    assert progress == ["queued", "in_progress"]  # progress emitted for each not-yet-complete


async def test_poll_stops_at_timeout_without_faking_success(monkeypatch):
    settings = get_settings()
    gh = GitHubClient(settings)
    gh.enabled = True

    async def _get_run(repo, run_id):
        return {"id": 9, "status": "in_progress", "conclusion": None, "url": "u"}

    monkeypatch.setattr(gh, "get_run", _get_run)
    monkeypatch.setattr(anyio, "sleep", lambda *_a, **_k: anyio.lowlevel.checkpoint())
    final = await gh.poll_run_to_completion("o/r", 9, timeout=0.01, interval=0.02)
    assert final["status"] == "in_progress" and final["conclusion"] is None  # honest, not "success"


# ── devops_execute polls the dispatched run id ────────────────────────────────────────────

class _NoopCG:
    def __init__(self, *a, **k): pass
    def __getattr__(self, _n):
        async def _f(*a, **k): return None
        return _f


class _Emitter:
    def __init__(self): self.console_lines = []
    async def step(self, *a, **k): pass
    async def token(self, *a, **k): pass
    async def console(self, stream, line): self.console_lines.append(line)
    async def error(self, *a, **k): pass


class _Repo:
    html_url = "https://github.com/acme/app"


class _FakeGH:
    enabled = True
    def __init__(self, final_ci, dispatched=True):
        self._final_ci = final_ci
        self._dispatched = dispatched
        self.polled_run_id = None
    async def ensure_repo(self, *a, **k): return _Repo()
    async def upsert_file(self, *a, **k): return {}
    async def dispatch_workflow(self, *a, **k): return True
    async def find_dispatched_run(self, repo, wf, branch, since):
        return {"id": 4242, "status": "queued", "conclusion": None, "url": "u"} if self._dispatched else None
    async def poll_run_to_completion(self, repo, run_id, **k):
        self.polled_run_id = run_id
        return self._final_ci


class _DisabledK8s:
    enabled = False


def _state():
    return {"run_id": "r1", "org_id": "o1", "context_id": "c1",
            "parsed_inputs": {"repo": "acme/app", "branch": "main", "env": "dev", "namespace": "default"}}


async def _run_execute(monkeypatch, gh):
    monkeypatch.setattr(devops_mod, "ContextGraph", _NoopCG)
    monkeypatch.setattr(devops_mod, "get_github", lambda s: gh)
    monkeypatch.setattr(devops_mod, "get_kubernetes", lambda s: _DisabledK8s())
    emitter = _Emitter()
    monkeypatch.setattr(devops_mod, "emitter_of", lambda cfg: emitter)
    out = await devops_mod.devops_execute(_state(), {})
    return out, emitter


async def test_execute_polls_the_dispatched_run_on_success(monkeypatch):
    gh = _FakeGH({"id": 4242, "status": "completed", "conclusion": "success", "url": "u"})
    out, _em = await _run_execute(monkeypatch, gh)
    assert gh.polled_run_id == 4242                       # polled the run it dispatched, by id
    assert out["outcome"]["status"] == "deployed"
    assert out["outcome"]["ci"]["conclusion"] == "success"


async def test_execute_fails_when_ci_concludes_failure(monkeypatch):
    gh = _FakeGH({"id": 4242, "status": "completed", "conclusion": "failure", "url": "u"})
    out, _em = await _run_execute(monkeypatch, gh)
    assert gh.polled_run_id == 4242
    assert out["outcome"]["status"] == "failed"           # a failed CI fails the pipeline honestly
    assert "failure" in out["outcome"]["error"]


async def test_execute_reports_honestly_when_run_not_visible(monkeypatch):
    gh = _FakeGH(final_ci=None, dispatched=False)          # dispatch accepted, run not yet visible
    out, _em = await _run_execute(monkeypatch, gh)
    assert gh.polled_run_id is None                        # nothing to poll → not faked
    assert out["outcome"]["status"] == "deployed"
    assert out["outcome"]["ci"]["status"] == "dispatched"
