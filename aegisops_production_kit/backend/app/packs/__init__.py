"""P4 capability packs (Redesign/00 §3, 02 §4/§9, 07 Phase 4 — the harness-first inversion).

Domains become THIN specialist layers that contribute knowledge, tools, and verification
strategies — never control flow. The P2 harness owns reasoning; the P3 engine owns
durability; P1 owns models. A pack is reviewable DATA + registrations (ADR-13: no
runtime-loaded code).

Boundaries this package HOLDS (07 Phase 4, P4 prompt):
- cloud-neutral orchestration: the harness/engine/policy/memory/model layers contain NO
  provider-specific logic; AWS/Azure/GCP/GitHub/K8s specifics live ONLY inside packs.
- reads first (07 P4.2): packs contribute READ tools to the harness INV registry; real
  cloud MUTATION stays the governed exec_loop/approval/P3 path (Terraform safety boundary
  untouched) — mutation capabilities are DECLARED (templates/day2 metadata), not executed
  here.
- dark launch (07 P4.3, risk #1): the pack read path is gated by `aegisops_capability_packs`
  (default off); the legacy `investigation.default_registry` remains the default. The
  production-spine cutover + cloudops.py dissolution ship only at proven eval parity.
"""

from .base import CapabilityPack, ToolSpec
from .registry import build_read_registry, configured_packs

__all__ = ["CapabilityPack", "ToolSpec", "build_read_registry", "configured_packs"]
