"""P0 security/preflight tests.

Contract (P0 task §16): detect secrets / tfstate / tfplan / SA-keys; NEVER print secret
values; the sandbox allowlist is an exact-path manifest (operator-declared 2026-08-09);
anything outside it fails. These tests report file paths and rule names ONLY.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).parents[1]
KIT = BACKEND.parent  # aegisops_production_kit (or /app in the api-test container)

# The operator-declared DEV/SANDBOX manifest — must stay in lockstep with .gitleaks.toml.
SANDBOX_ALLOWLIST = (
    r"(^|[\\/])infra[\\/]secrets[\\/]gcp-sa\.json$",
    r"(^|[\\/])infra[\\/]secrets[\\/]aegisops-before-wipe-20260719\.dump$",
    r"(^|[\\/])\.env$",
    r"(^|[\\/])infra[\\/]terraform-workspaces[\\/].+\.tfstate(\.backup)?$",
    r"(^|[\\/])infra[\\/]terraform-workspaces[\\/].+\.tfplan$",
)

TRACKED_DENY = (
    (r"(^|/)\.env(\..+)?$", "dotenv", (r"\.env\.example$",)),
    (r"\.tfstate(\.backup)?$", "terraform-state", ()),
    (r"\.tfplan$", "terraform-plan", ()),
    (r"-sa\.json$", "service-account-key", ()),
    (r"(^|/)infra/secrets/", "secrets-dir", ()),
)


def _gitignore() -> str:
    p = KIT / ".gitignore"
    if not p.exists():
        pytest.skip(".gitignore not visible from this tier")
    return p.read_text(encoding="utf-8")


def test_gitignore_guards_every_secret_shaped_pattern():
    gi = _gitignore()
    for needle in (".env", "*.tfstate", "*.tfplan", "infra/secrets/",
                   "*.egg-info/", "llm_usage_spill"):
        assert needle in gi, f".gitignore lost the {needle!r} guard"


def test_gitleaks_manifest_is_exact_paths_only():
    """The scanner is never weakened to make the sandbox pass: every allowlist entry is
    an exact path/anchored pattern from the operator declaration — no directory-wide or
    wildcard-rule escapes beyond the declared artifacts."""
    p = KIT / ".gitleaks.toml"
    if not p.exists():
        pytest.skip(".gitleaks.toml not visible from this tier")
    import tomllib
    cfg = tomllib.loads(p.read_text(encoding="utf-8"))
    paths = cfg["allowlist"]["paths"]
    assert sorted(paths) == sorted(SANDBOX_ALLOWLIST), (
        "sandbox manifest drifted from the operator declaration — adding an entry is a "
        "change-management event")
    rule_ids = {r["id"] for r in cfg.get("rules", [])}
    for required in ("aegisops-terraform-state", "aegisops-terraform-plan",
                     "aegisops-dotenv", "aegisops-service-account-json"):
        assert required in rule_ids


def test_no_secret_shaped_path_is_tracked_by_git():
    """Committed-tree leak check (paths only — values are never read, never printed)."""
    try:
        out = subprocess.run(["git", "ls-files"], cwd=KIT, capture_output=True,
                             text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("git unavailable in this tier")
    if out.returncode != 0:
        pytest.skip("not a git checkout in this tier")
    violations: list[tuple[str, str]] = []
    for path in out.stdout.splitlines():
        for pattern, rule, excludes in TRACKED_DENY:
            if re.search(pattern, path) and not any(re.search(x, path) for x in excludes):
                violations.append((path, rule))
    assert not violations, f"secret-shaped files tracked by git (path, rule): {violations}"


def test_redaction_masks_synthetic_credentials_without_echoing_them():
    from app.security.redaction import redact
    # AWS's own documented example key id (AKIA + exactly 16 chars — the real shape).
    synthetic = "aws key AKIAIOSFODNN7EXAMPLE and header Authorization: Bearer abc.def.ghi"
    out = redact(synthetic)
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "abc.def.ghi" not in out


def test_scanner_tooling_never_prints_values_by_construction():
    """CI runs gitleaks with --redact; the local pre-commit hook only names paths. This
    pins the flags so a config edit can't silently start echoing secrets."""
    ci = KIT / ".github" / "workflows" / "ci.yml"
    if not ci.exists():
        pytest.skip("workflow file not visible from this tier")
    text = ci.read_text(encoding="utf-8")
    assert "--redact" in text and "gitleaks" in text


if sys.platform != "win32":
    # Path-separator note: the allowlist regexes accept both separators so the manifest
    # comparison above stays byte-identical across host (Windows) and container (Linux).
    pass
