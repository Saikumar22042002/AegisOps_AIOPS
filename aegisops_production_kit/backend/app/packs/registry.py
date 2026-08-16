"""Pack registry → harness read registry (P4 — 02 §4, 07 P4.2).

Assembles the configured capability packs and builds the frozen read-only tool registry the
P2 harness INV loop drives. This is the inversion for READ paths: the harness's tool surface
becomes the packs' read tools, cloud-neutrally — the harness never names a provider, the
packs own all provider specifics.

Read-only-by-construction is doubly enforced: only `effect == read` specs are registered,
AND the underlying investigation `ToolRegistry` still trips its mutation-marker denylist —
so a mis-declared mutation tool cannot slip into the read surface.
"""

from __future__ import annotations

import structlog

from ..agents.investigation import ToolRegistry
from ..settings import Settings
from .base import CapabilityPack

log = structlog.get_logger(__name__)

# Pack builders, registered as data (ADR-13: no runtime-loaded code). Adding a provider is a
# builder entry + a pack module — zero change to the harness/engine/policy layers.
_BUILDERS = {
    "cloudops.aws": lambda s: __import__("app.packs.cloudops.aws", fromlist=["build"]).build(s),
    "cloudops.azure": lambda s: __import__("app.packs.cloudops.azure", fromlist=["build"]).build(s),
    "cloudops.gcp": lambda s: __import__("app.packs.cloudops.gcp", fromlist=["build"]).build(s),
    "sreops.k8s": lambda s: __import__("app.packs.sreops.k8s", fromlist=["build"]).build(s),
    "devops.github": lambda s: __import__("app.packs.devops.github", fromlist=["build"]).build(s),
}


def all_packs(settings: Settings) -> list[CapabilityPack]:
    return [b(settings) for b in _BUILDERS.values()]


def configured_packs(settings: Settings) -> list[CapabilityPack]:
    """Packs whose provider has credentials — an unconfigured provider lists but never
    contributes callable tools (parity honesty: no fake abstraction)."""
    out = []
    for pack in all_packs(settings):
        try:
            if pack.enabled(settings):
                out.append(pack)
        except Exception as exc:  # noqa: BLE001 — a probe error means "not configured"
            log.warning("packs.enabled_probe_failed", pack=pack.name, error=str(exc))
    return out


def build_read_registry(settings: Settings, *, packs: list[CapabilityPack] | None = None
                        ) -> ToolRegistry:
    """The frozen read-only registry the harness INV loop drives, sourced from packs."""
    reg = ToolRegistry()
    for pack in (packs if packs is not None else configured_packs(settings)):
        for tool in pack.read_tools():
            # The investigation registry asserts read-only at registration (denylist) —
            # a mutation-named tool would raise here, which is the safety we want.
            reg.register(tool.name, tool.description, tool.fn)
    return reg.freeze()


def capability_catalog(settings: Settings) -> list[dict]:
    """A provider-neutral view of what each pack can do — for the objective model, the
    parity matrix, and the frontend capability display."""
    out = []
    for pack in all_packs(settings):
        out.append({
            "pack": pack.name, "provider": pack.provider, "domain": pack.domain,
            "configured": _safe_enabled(pack, settings),
            "read": sorted({t.family for t in pack.tools if t.effect == "read"}),
            "mutation": sorted({t.family for t in pack.tools if t.effect == "mutation"}),
            "templates": list(pack.templates), "day2": list(pack.day2),
        })
    return out


def _safe_enabled(pack: CapabilityPack, settings: Settings) -> bool:
    try:
        return bool(pack.enabled(settings))
    except Exception:  # noqa: BLE001
        return False
