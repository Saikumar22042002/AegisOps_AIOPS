"""Stale-waiver guard (owner-binding, MS-7+): a scanner waiver whose reason leans on a
future MODSEED item ("arrives with MS-N") must be REMOVED in that item's commit — the
option exists, the waiver dies with it. This test makes a stale one a RED suite:
no waiver in any .checkov.yaml / .tfsec/config.yml may reference an MS item that
FIX.md §8 marks done.
"""

from __future__ import annotations

import re
from pathlib import Path

_MS_REF = re.compile(r"\bMS-(\d+)\b")
_DONE_ROW = re.compile(r"^\|\s*MS-(\d+)\s*\|.*\*\*done\*\*", re.MULTILINE)


def _find_up(name: str) -> Path:
    """Walk parents of this file, then the container mount point. Hard-fail if absent:
    a guard that silently skips is no guard."""
    for base in Path(__file__).resolve().parents:
        cand = base / name
        if cand.is_file():
            return cand
    mounted = Path("/app") / name
    if mounted.is_file():
        return mounted
    raise FileNotFoundError(
        f"{name} not reachable — the stale-waiver guard cannot run "
        "(api-test mounts ../FIX.md to /app/FIX.md; keep that mount)"
    )


def _workspaces_dir() -> Path:
    for base in Path(__file__).resolve().parents:
        cand = base / "infra" / "terraform-workspaces"
        if cand.is_dir():
            return cand
    raise FileNotFoundError("infra/terraform-workspaces not found")


def test_no_waiver_references_a_done_ms_item():
    fix = _find_up("FIX.md").read_text(encoding="utf-8")
    done = {int(m) for m in _DONE_ROW.findall(fix)}
    assert done, "FIX.md §8 parse found no done MS rows — the done-row regex is broken"

    stale: list[str] = []
    for cfg in sorted(_workspaces_dir().glob("*/.checkov.yaml")) + sorted(
        _workspaces_dir().glob("*/.tfsec/config.yml")
    ):
        for lineno, line in enumerate(cfg.read_text(encoding="utf-8").splitlines(), 1):
            for m in _MS_REF.finditer(line):
                n = int(m.group(1))
                if n in done:
                    stale.append(f"{cfg.parent.name}/{cfg.name}:{lineno} references MS-{n} "
                                 f"(marked done in FIX.md §8): {line.strip()}")
    assert not stale, (
        "STALE SCANNER WAIVERS — the MS item shipped, so the waiver must be removed "
        "(or re-justified without the MS reference):\n" + "\n".join(stale)
    )


def test_ms_references_in_waivers_point_at_real_items():
    """A waiver may only lean on MS-1..13 — anything else is a typo that would dodge
    the stale check forever."""
    bad: list[str] = []
    for cfg in sorted(_workspaces_dir().glob("*/.checkov.yaml")) + sorted(
        _workspaces_dir().glob("*/.tfsec/config.yml")
    ):
        for lineno, line in enumerate(cfg.read_text(encoding="utf-8").splitlines(), 1):
            for m in _MS_REF.finditer(line):
                if not 1 <= int(m.group(1)) <= 13:
                    bad.append(f"{cfg.parent.name}/{cfg.name}:{lineno}: {line.strip()}")
    assert not bad, "Waivers reference nonexistent MODSEED items:\n" + "\n".join(bad)
