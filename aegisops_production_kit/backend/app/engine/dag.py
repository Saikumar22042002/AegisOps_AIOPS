"""Step + Workflow contracts and wave compilation (P3.1 — Redesign/05 §5, 06 §8.1).

`compile_workflow` turns a validated goal-DAG draft into a durable `Workflow`: an ordered
list of `Step`s assigned to dependency-ordered WAVES, with a disjoint-output compile check
(steps in the same wave must not target the same resource identity — the frozen wave-safety
rule, 07 P3.1). Catalog validation reuses the exec_loop guard verbatim (07: "compile
closures kept verbatim") so a non-catalog template can never enter a workflow.

Compile is pure and deterministic; nothing here executes or persists.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..agents import exec_loop


@dataclass(frozen=True)
class Step:
    id: str
    kind: str                       # module | day2 | k8s | read | gate
    name: str
    template_key: str | None
    params: dict
    depends_on: tuple[str, ...] = ()
    wave: int = 0
    # idempotency identity: stable across restarts so a completed step is never re-run.
    idempotency_key: str = ""
    # the resource identity this step targets (for the disjoint-output wave check)
    output_id: str | None = None


@dataclass(frozen=True)
class Workflow:
    steps: tuple[Step, ...]
    waves: tuple[tuple[str, ...], ...]   # step ids per wave, in execution order

    def step(self, sid: str) -> Step:
        return next(s for s in self.steps if s.id == sid)


class CompileError(Exception):
    """A draft that cannot become a safe workflow (cycle, non-catalog, wave collision)."""


def _layer(steps: list[dict]) -> list[list[str]]:
    """Kahn layering: each wave is the set of steps whose deps are all satisfied by
    earlier waves. Raises on a dependency cycle."""
    by_id = {s["id"]: s for s in steps}
    remaining = set(by_id)
    done: set[str] = set()
    waves: list[list[str]] = []
    while remaining:
        ready = [sid for sid in remaining
                 if all(d in done for d in (by_id[sid].get("depends_on") or []))]
        if not ready:
            raise CompileError("dependency cycle or unknown dependency in goal DAG")
        ready.sort()
        waves.append(ready)
        done |= set(ready)
        remaining -= set(ready)
    return waves


def _disjoint_output_check(waves: list[list[str]], by_id: dict[str, dict]) -> None:
    """No two steps in the SAME wave may target the same resource identity (07 P3.1)."""
    for w, ids in enumerate(waves):
        seen: dict[str, str] = {}
        for sid in ids:
            out = by_id[sid].get("output_id") or by_id[sid].get("name")
            if out in seen:
                raise CompileError(
                    f"wave {w}: steps {seen[out]!r} and {sid!r} both target {out!r} — "
                    "concurrent steps must have disjoint outputs")
            seen[out] = sid


def compile_workflow(draft: list[dict], *, run_id: str) -> Workflow:
    """Compile a goal-DAG draft into a durable, wave-scheduled Workflow.

    `draft` = [{id, kind, name, template_key, params, depends_on?, output_id?}, …].
    Reuses `exec_loop.validate_dag` for catalog validation (mutation steps only touch the
    approved template catalog — the constitution, 00 §7)."""
    if not draft:
        raise CompileError("empty goal DAG")
    # Catalog guard (verbatim reuse): module steps must reference an approved template.
    module_steps = [{"template_key": s.get("template_key"), **s}
                    for s in draft if s.get("kind", "module") == "module"]
    if module_steps:
        err = exec_loop.validate_dag(module_steps)
        if err:
            raise CompileError(err)
    by_id = {s["id"]: s for s in draft}
    if len(by_id) != len(draft):
        raise CompileError("duplicate step ids in goal DAG")
    waves = _layer(draft)
    _disjoint_output_check(waves, by_id)
    wave_of = {sid: w for w, ids in enumerate(waves) for sid in ids}
    steps = tuple(
        Step(id=s["id"], kind=s.get("kind", "module"), name=s.get("name", s["id"]),
             template_key=s.get("template_key"), params=dict(s.get("params") or {}),
             depends_on=tuple(s.get("depends_on") or ()), wave=wave_of[s["id"]],
             idempotency_key=f"dstep:{run_id}:{s['id']}",
             output_id=s.get("output_id") or s.get("name"))
        for s in draft)
    return Workflow(steps=steps, waves=tuple(tuple(w) for w in waves))
