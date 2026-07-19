"""STAB P0-1 — the shared Terraform plugin cache must never be poisoned or fatal.

Live failure (Screenshots/13.png, 2026-07-19): every GCS plan hard-failed with
`terraform init … /app/.tf-plugin-cache/…/google/5.45.2/linux_amd64/LICENSE.txt:
permission denied`, and warm S3/azure plans regressed to 2m28s/1m30s (audited baseline
S3 ~14s). Root cause: commit 1d27bd4 mounted the api's `tfplugins` volume into
`api-test`, which runs as root — root-written provider files are unreadable by the
non-root api (uid 10001).

Two defenses, each tested at the layer that failed:
1. Structure (compose): a root-running service never mounts the api's plugin-cache
   volume again — api-test gets its own `tfplugins-test` volume.
2. Resilience (runner): if the cache itself is what's broken, `init` retries once
   WITHOUT the cache (loud log) instead of failing the run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app.settings import Settings
from app.tools.terraform import TerraformError, TerraformRunner

# ── 1. compose invariant (would have caught 1d27bd4) ──

_NAMED_VOL = re.compile(r"^[\w-]+$")


def _find_up(name: str) -> Path:
    """Walk parents of this file, then the container mount point (api-test ro-mounts the
    compose files to /app). Hard-fail if absent — a guard that silently skips is no guard."""
    for base in Path(__file__).resolve().parents:
        cand = base / name
        if cand.is_file():
            return cand
    mounted = Path("/app") / name
    if mounted.is_file():
        return mounted
    raise FileNotFoundError(
        f"{name} not reachable — the P0-1 compose guard cannot run "
        "(api-test mounts ./docker-compose*.yml to /app; keep those mounts)")


def _merged_services() -> dict:
    services: dict[str, dict] = {}
    for name in ("docker-compose.yml", "docker-compose.override.yml"):
        doc = yaml.safe_load(_find_up(name).read_text(encoding="utf-8")) or {}
        for svc_name, svc in (doc.get("services") or {}).items():
            merged = services.setdefault(svc_name, {"volumes": []})
            for k, v in (svc or {}).items():
                if k == "volumes":
                    merged["volumes"].extend(v or [])
                elif isinstance(v, dict) and isinstance(merged.get(k), dict):
                    merged[k] = {**merged[k], **v}  # compose merges mappings per-key
                else:
                    merged[k] = v
    return services


def _named_volumes(svc: dict) -> set[str]:
    out = set()
    for v in svc.get("volumes", []):
        if isinstance(v, str):
            src = v.split(":")[0]
            if _NAMED_VOL.match(src):
                out.add(src)
    return out


def test_api_plugin_cache_never_mounted_by_a_root_service():
    services = _merged_services()
    assert "tfplugins" in _named_volumes(services["api"]), \
        "the api's plugin-cache volume moved — update this guard with it"
    offenders = {
        name for name, svc in services.items()
        if str(svc.get("user", "")).strip() in ("root", "0")
        and "tfplugins" in _named_volumes(svc)
    }
    assert not offenders, (
        f"{sorted(offenders)} run as root and mount the api's plugin cache — root-written "
        "provider files are unreadable by the non-root api (STAB P0-1, regression of 1d27bd4)")


def test_api_test_keeps_its_own_persistent_cache():
    """The 1d27bd4 intent (no ~700MB re-download per fresh test container) survives —
    on api-test's OWN volume, never the api's."""
    services = _merged_services()
    vols = _named_volumes(services["api-test"])
    assert "tfplugins" not in vols
    assert "tfplugins-test" in vols
    assert (services["api-test"].get("environment") or {}).get("TF_PLUGIN_CACHE_DIR"), \
        "api-test lost its plugin cache entirely — the download wedge returns"


# ── 2. init falls back without the cache when the cache itself is broken ──

class _R:
    def __init__(self, returncode: int, stdout=None, stderr=None):
        self.returncode = returncode
        self.stdout = stdout or []
        self.stderr = stderr or []


_PERM_DENIED = _R(1, stderr=[
    "Error: Failed to install provider",
    "Error while installing hashicorp/google v5.45.2: open "
    "/app/.tf-plugin-cache/registry.terraform.io/hashicorp/google/5.45.2/"
    "linux_amd64/LICENSE.txt: permission denied",
])


def _runner(tmp_path, monkeypatch, script: list[_R], cache_dir="/app/.tf-plugin-cache"):
    (tmp_path / "demo-null").mkdir()
    s = Settings(_env_file=None, terraform_workspaces_dir=str(tmp_path),
                 tf_plugin_cache_dir=cache_dir)
    r = TerraformRunner("demo-null", s)
    calls: list[dict] = []

    class _ScriptedConsole:
        def __init__(self, cwd, env):
            self.env = env

        async def run(self, args, on_line=None, timeout=None):
            calls.append(dict(self.env))
            return script.pop(0)

    monkeypatch.setattr("app.tools.terraform.CommandConsole", _ScriptedConsole)
    return r, calls


async def test_poisoned_cache_falls_back_to_no_cache_init(tmp_path, monkeypatch):
    r, calls = _runner(tmp_path, monkeypatch, script=[_PERM_DENIED, _R(0)])
    out = await r.init()
    assert out["ok"] is True
    assert len(calls) == 2, "exactly one no-cache retry"
    assert calls[0].get("TF_PLUGIN_CACHE_DIR") == "/app/.tf-plugin-cache"
    assert "TF_PLUGIN_CACHE_DIR" not in calls[1], \
        "the retry must run WITHOUT the broken cache"


async def test_unrelated_init_failure_never_retries(tmp_path, monkeypatch):
    boom = _R(1, stderr=["Error: Invalid provider configuration", "something else broke"])
    r, calls = _runner(tmp_path, monkeypatch, script=[boom])
    with pytest.raises(TerraformError):
        await r.init()
    assert len(calls) == 1, "no silent retry on non-cache failures"


async def test_permission_error_outside_the_cache_never_retries(tmp_path, monkeypatch):
    boom = _R(1, stderr=["Error: open /app/infra/terraform-workspaces/x/main.tf: permission denied"])
    r, calls = _runner(tmp_path, monkeypatch, script=[boom])
    with pytest.raises(TerraformError):
        await r.init()
    assert len(calls) == 1, "the fallback keys on the CACHE path, not any permission error"


async def test_no_cache_configured_means_no_fallback(tmp_path, monkeypatch):
    boom = _R(1, stderr=["open /app/.tf-plugin-cache/x: permission denied"])
    r, calls = _runner(tmp_path, monkeypatch, script=[boom], cache_dir="")
    with pytest.raises(TerraformError):
        await r.init()
    assert len(calls) == 1


# ── 3. TF_DATA_DIR relocation (the 9p/OneDrive latency half of P0-1) ──
# Measured live 2026-07-19: aws-s3 plan 84-94s with .terraform on the 9p workspaces bind
# mount → 5s with TF_DATA_DIR on the native tfstate volume (providers symlink from cache).

def _data_runner(tmp_path, data_root: str):
    (tmp_path / "demo-null").mkdir(exist_ok=True)
    s = Settings(_env_file=None, terraform_workspaces_dir=str(tmp_path),
                 tf_data_root=data_root)
    return TerraformRunner("demo-null", s)


def test_env_carries_per_module_data_dir_when_root_configured(tmp_path):
    r = _data_runner(tmp_path, data_root="/native/tfdata")
    env = r._env()
    assert env["TF_DATA_DIR"] == "/native/tfdata/demo-null"
    assert "TF_DATA_DIR" not in _data_runner(tmp_path, data_root="")._env(), \
        "unset root keeps the pre-P0-1 module-dir behavior"


def test_is_initialized_checks_the_data_dir_when_configured(tmp_path):
    root = tmp_path / "tfdata"
    r = _data_runner(tmp_path, data_root=str(root))
    (tmp_path / "demo-null" / ".terraform.lock.hcl").write_text("# lock")
    # module-dir .terraform must NOT count once the data root is configured
    (tmp_path / "demo-null" / ".terraform").mkdir()
    assert r._is_initialized() is False
    (root / "demo-null").mkdir(parents=True)
    assert r._is_initialized() is True


async def test_first_relocated_init_auto_approves_local_state_adoption(tmp_path, monkeypatch):
    """Fresh TF_DATA_DIR over legacy local state hits the residual-backend migration
    prompt (-input=false turns it into a hard error — the LAT-known failure). local→local
    is adoption of the same terraform.tfstate.d files → -migrate-state -force-copy."""
    (tmp_path / "demo-null").mkdir()
    s = Settings(_env_file=None, terraform_workspaces_dir=str(tmp_path),
                 tf_data_root=str(tmp_path / "tfdata"))
    r = TerraformRunner("demo-null", s)
    calls: list[list[str]] = []

    class _C:
        def __init__(self, cwd, env): ...

        async def run(self, args, on_line=None, timeout=None):
            calls.append(list(args))
            return _R(0)

    monkeypatch.setattr("app.tools.terraform.CommandConsole", _C)
    await r.init()
    assert "-migrate-state" in calls[0] and "-force-copy" in calls[0]

    # remote mode keeps A3 semantics untouched — no auto-migration flags
    calls.clear()
    s2 = Settings(_env_file=None, terraform_workspaces_dir=str(tmp_path),
                  tf_data_root=str(tmp_path / "tfdata"),
                  aegisops_tf_backend="remote", tf_state_bucket="b", tf_state_region="us-east-1")
    r2 = TerraformRunner("demo-null", s2)
    await r2.init()
    assert "-migrate-state" not in calls[0] and "-force-copy" not in calls[0]


def test_compose_puts_the_data_root_on_the_native_state_volume():
    services = _merged_services()
    for svc in ("api", "api-b"):
        envd = services[svc].get("environment") or {}
        root = str(envd.get("TF_DATA_ROOT", ""))
        assert root.startswith("/app/.terraform-state/"), \
            f"{svc}: TF_DATA_ROOT must live under the native tfstate volume, got '{root}'"
        assert any(str(v).startswith("tfstate:/app/.terraform-state")
                   for v in services[svc].get("volumes", [])), \
            f"{svc}: the tfstate named volume mount moved — TF_DATA_ROOT would land on 9p"
