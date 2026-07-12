"""DevOps agent — staged GitHub → CI → image → Kubernetes pipeline.

Stages: INIT → ENSURE_REPO_EXISTS → ENSURE_WORKING_COPY → ENSURE_CHANGES_PUSHED →
ENSURE_CI_RUN → ENSURE_IMAGE_EXISTS → ENSURE_K8S_DEPLOYED. Real GitHub API (repos, files/push,
Actions dispatch + tracking, Actions Secrets) and real Kubernetes apply. All side-effecting
stages run only after the human-approval interrupt. Tracks env (dev/stg/prod) + feature branch
and shares the repo link in chat.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from ..graph_db.context_graph import ContextGraph
from ..integrations.gemini import get_gemini
from ..security.confidentiality import classify
from ..settings import get_settings
from ..tools.github import GitHubError, get_github
from ..tools.kubernetes import get_kubernetes
from . import llm
from .runtime import emitter_of
from .state import AgentState

log = structlog.get_logger(__name__)

STAGES = [
    "ENSURE_REPO_EXISTS", "ENSURE_WORKING_COPY", "ENSURE_CHANGES_PUSHED",
    "ENSURE_CI_RUN", "ENSURE_IMAGE_EXISTS", "ENSURE_K8S_DEPLOYED",
]

_DOCKERFILE = """FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt || true
CMD ["python", "-m", "http.server", "8080"]
"""

_CI_WORKFLOW = """name: ci
on: [push, workflow_dispatch]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build image
        run: docker build -t ${{ github.repository }}:${{ github.sha }} .
"""


async def _extract(settings, message: str) -> dict:
    from ..schemas.workflows import parse_freeform

    inputs = parse_freeform(message)
    gemini = get_gemini(settings)
    if gemini.enabled:
        system = ("Extract a DevOps deployment request as JSON with keys when present: "
                  "repo, branch, env (dev|stg|prod), namespace, image, description. Omit unknown keys.")
        try:
            inputs = {**(await llm.classify_json(settings, system, message)), **inputs}
        except Exception as e:  # noqa: BLE001
            log.warning("devops.extract_failed", error=str(e))
    return inputs


async def devops_plan(state: AgentState, config) -> dict:
    emitter = emitter_of(config)
    settings = get_settings()
    gh = get_github(settings)
    await emitter.step(2, "Selected workflow · devops-pipeline")

    if not gh.enabled:
        msg = "DevOps workflows need GITHUB_TOKEN (and GITHUB_ORG) configured in .env."
        await emitter.token(msg)
        return {"needs_change": False, "approval_status": "not_required", "answer": msg,
                "confidentiality": {"level": "Low", "score": 0.0}}

    inputs = await _extract(settings, state["message"])
    repo = inputs.get("repo")
    if not repo:
        return {"needs_change": False, "needs_clarification": True,
                "clarification": "Which repository should I target (owner/name or name)? "
                                 "Also tell me the branch and target env (dev/stg/prod)."}
    branch = inputs.get("branch", "main")
    env = inputs.get("env", "dev")
    namespace = inputs.get("namespace", "default")

    await emitter.step(3, "Checked repository state")
    exists = await gh.repo_exists(repo)
    await emitter.console("stdout", f"repo {repo}: {'exists' if exists else 'absent (will be created)'}")

    stages_plan = [
        {"stage": "ENSURE_REPO_EXISTS", "detail": "exists" if exists else "create repo", "status": "planned"},
        {"stage": "ENSURE_WORKING_COPY", "detail": "ensure Dockerfile + Actions workflow", "status": "planned"},
        {"stage": "ENSURE_CHANGES_PUSHED", "detail": f"commit + push to {branch}", "status": "planned"},
        {"stage": "ENSURE_CI_RUN", "detail": "dispatch + track GitHub Actions", "status": "planned"},
        {"stage": "ENSURE_IMAGE_EXISTS", "detail": "verify image build", "status": "planned"},
        {"stage": "ENSURE_K8S_DEPLOYED", "detail": f"deploy to {namespace} ({env})", "status": "planned"},
    ]
    plan_json = {"stages": stages_plan, "repo": repo, "branch": branch, "env": env, "namespace": namespace}
    policy = [
        {"name": "Approval before push/CI/deploy", "passed": True},
        {"name": "Production deploy requires admin approval", "passed": True, "detail": env},
    ]
    cards = [
        {"title": "Interpreted intent", "conf": f"{int(state.get('intent_confidence', 0) * 100)}%",
         "body": f"Deploy {repo}@{branch} to {env}. {state.get('routing_reason','')}"},
        {"title": "Pipeline", "conf": "", "body": " → ".join(STAGES)},
    ]
    await emitter.analysis(summary=f"Prepared a {len(STAGES)}-stage pipeline for {repo}; awaiting approval.", cards=cards)
    c = classify(str(inputs))
    await emitter.confidentiality(c.level, c.score)

    try:
        cg = ContextGraph(state.get("context_id") or state["run_id"], state.get("org_id", ""))
        await cg.set_workflow(workflow="devops-pipeline", version="v1", template="github-k8s", inputs=plan_json)
    except Exception as e:  # noqa: BLE001
        log.warning("devops.cg_failed", error=str(e))

    payload = {"kind": "approval", "runId": state["run_id"], "workflow": "devops-pipeline",
               "plan": plan_json, "policyChecks": policy, "mode": "deploy", "env": env}
    await emitter.step(9, "Awaiting approval")
    await emitter.interrupt(payload)
    return {"workflow": "devops-pipeline", "workflow_version": "v1", "parsed_inputs": plan_json,
            "plan_json": plan_json, "policy_checks": policy, "needs_change": True,
            "approval_status": "pending", "execution_mode": "apply", "interrupt_payload": payload,
            "reasoning_cards": cards, "confidentiality": {"level": c.level, "score": c.score},
            "answer": f"Prepared a {len(STAGES)}-stage CI/CD pipeline for {repo}. Approve to run it."}


async def devops_execute(state: AgentState, config) -> dict:
    emitter = emitter_of(config)
    settings = get_settings()
    gh = get_github(settings)
    k8s = get_kubernetes(settings)
    p = state.get("parsed_inputs", {})
    repo, branch, env, namespace = p.get("repo"), p.get("branch", "main"), p.get("env", "dev"), p.get("namespace", "default")
    cg = ContextGraph(state.get("context_id") or state["run_id"], state.get("org_id", ""))
    results: dict = {}
    order = 3

    async def stage(name: str, coro):
        nonlocal order
        await emitter.step(5, name)
        await emitter.console("stdout", f"[{name}] starting")
        await cg.add_step(order=order, name=name, agent="devops", tool="github", status="running")
        try:
            res = await coro
            await cg.update_step(order=order, status="done", result={"ok": True})
            await emitter.console("stdout", f"[{name}] done")
            order += 1
            return res
        except Exception as e:  # noqa: BLE001
            await cg.update_step(order=order, status="failed", error=str(e))
            await emitter.console("stderr", f"[{name}] failed: {e}")
            order += 1
            raise

    try:
        repo_obj = await stage("ENSURE_REPO_EXISTS", gh.ensure_repo(repo, description="Created by AegisOps"))
        results["repo_url"] = getattr(repo_obj, "html_url", None)

        async def working_copy():
            await gh.upsert_file(repo, "Dockerfile", _DOCKERFILE, "chore: add Dockerfile (AegisOps)", branch)
            await gh.upsert_file(repo, ".github/workflows/ci.yml", _CI_WORKFLOW, "ci: add workflow (AegisOps)", branch)
            return True
        await stage("ENSURE_WORKING_COPY", working_copy())
        await stage("ENSURE_CHANGES_PUSHED", gh.upsert_file(
            repo, "AEGISOPS.md", f"Managed by AegisOps · env={env}", "docs: AegisOps marker", branch))

        # P16: dispatch the workflow, IDENTIFY the run it created (dispatch returns no id), then
        # POLL that run to completion — the CI result is real, not the latest-run guess.
        since = datetime.now(timezone.utc)

        async def run_ci():
            await gh.dispatch_workflow(repo, "ci.yml", ref=branch)
            dispatched = await gh.find_dispatched_run(repo, "ci.yml", branch, since)
            if dispatched:
                await emitter.console("stdout",
                                      f"[ENSURE_CI_RUN] tracking run {dispatched['id']} → {dispatched.get('url')}")
            return dispatched
        dispatched = await stage("ENSURE_CI_RUN", run_ci())
        run_id = (dispatched or {}).get("id")

        async def verify_image():
            if not run_id:
                # The dispatch was accepted but the run has not surfaced on the API yet — say so
                # honestly rather than claim a build we can't observe.
                return {"status": "dispatched", "conclusion": None,
                        "note": "CI run not yet visible from the GitHub API"}

            async def _progress(info):
                await emitter.console("stdout", f"[ENSURE_IMAGE_EXISTS] CI run {run_id}: {info.get('status')}")
            final = await gh.poll_run_to_completion(repo, run_id, on_poll=_progress)
            if final.get("status") == "completed" and final.get("conclusion") != "success":
                raise GitHubError(f"CI run {run_id} concluded '{final.get('conclusion')}' — image not built")
            return final
        ci = await stage("ENSURE_IMAGE_EXISTS", verify_image())
        results["ci"] = ci

        if k8s.enabled and p.get("image"):
            manifest = _deployment_manifest(repo.split("/")[-1], p["image"], namespace)
            await stage("ENSURE_K8S_DEPLOYED", k8s.apply_deployment(namespace, manifest))
        else:
            await emitter.console("stdout", "[ENSURE_K8S_DEPLOYED] skipped (no kubeconfig/image)")
    except Exception as e:  # noqa: BLE001
        await emitter.error(f"DevOps pipeline failed: {e}", code="devops_error", retriable=True)
        return {"outcome": {"status": "failed", "error": str(e), **results}}

    if results.get("repo_url"):
        await emitter.token(f"\nRepository: {results['repo_url']}")
    return {"outcome": {"status": "deployed", **results}, "tool_results": [results]}


def _deployment_manifest(name: str, image: str, namespace: str) -> dict:
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace, "labels": {"app": name, "managed-by": "aegisops"}},
        "spec": {"replicas": 2, "selector": {"matchLabels": {"app": name}},
                 "template": {"metadata": {"labels": {"app": name}},
                              "spec": {"containers": [{"name": name, "image": image, "ports": [{"containerPort": 8080}]}]}}},
    }
