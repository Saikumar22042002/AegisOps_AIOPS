"""P1.9 — provider import boundary (07 P1.9; Redesign/02 §9 import law).

Static AST enforcement, dependency-free and CI-fast:
1. provider SDK imports (google.genai / anthropic / openai) live ONLY in
   `app/llm/adapters/*` — generic application code depends on canonical contracts;
2. `langgraph` imports stay confined to the six modules that hold the orchestration
   spine today (ADR-04: reduced + isolated; a NEW module importing langgraph is
   architecture drift, not convenience);
3. the llm substrate never imports from the agent layer (`app.llm` is BELOW
   `app.agents` — the P2 harness will depend on it, never the reverse).

The formal contract also exists as `.importlinter` for the lint-imports tool; this
test is the always-on enforcement that needs no extra dependency.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"

SDK_ROOTS = {"google", "anthropic", "openai"}
SDK_ALLOWED_UNDER = ("app/llm/adapters/",)

LANGGRAPH_ALLOWED = {
    "app/agents/approval.py", "app/agents/checkpointer.py", "app/agents/exec_loop.py",
    "app/agents/graph.py", "app/agents/runner.py", "app/agents/state.py",
}


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append(node.module)
    return out


def _app_files() -> list[Path]:
    return [p for p in APP.rglob("*.py") if "__pycache__" not in p.parts]


def _rel(p: Path) -> str:
    return p.relative_to(BACKEND).as_posix()


def test_provider_sdks_only_inside_adapters():
    violations = []
    for f in _app_files():
        rel = _rel(f)
        if any(rel.startswith(prefix) for prefix in SDK_ALLOWED_UNDER):
            continue
        for mod in _imports(f):
            if mod.split(".")[0] in SDK_ROOTS:
                # `google.genai` is the SDK; `google.cloud.*` clients are cloud READ
                # tools (a different boundary, owned by the tool registry — 05 §1).
                if mod.split(".")[0] == "google" and not mod.startswith("google.genai"):
                    continue
                violations.append(f"{rel}: imports {mod}")
    assert not violations, (
        "provider SDK imports outside app/llm/adapters/ (P1.9):\n" + "\n".join(violations))


def test_langgraph_confined_to_the_spine_modules():
    violations = []
    for f in _app_files():
        rel = _rel(f)
        for mod in _imports(f):
            if mod.split(".")[0] == "langgraph" and rel not in LANGGRAPH_ALLOWED:
                violations.append(f"{rel}: imports {mod}")
    assert not violations, (
        "langgraph spread beyond the isolated spine (ADR-04):\n" + "\n".join(violations))


def test_llm_substrate_never_imports_the_agent_layer():
    violations = []
    for f in (APP / "llm").rglob("*.py"):
        if "__pycache__" in f.parts:
            continue
        for mod in _imports(f):
            if mod.startswith("app.agents") or ".agents" in mod:
                violations.append(f"{_rel(f)}: imports {mod}")
    assert not violations, (
        "app/llm must sit BELOW the agent layer (02 §9 import law):\n"
        + "\n".join(violations))
