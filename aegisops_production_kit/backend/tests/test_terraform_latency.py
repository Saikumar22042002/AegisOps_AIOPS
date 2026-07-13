"""LAT — skip `terraform init` when already initialized + shared provider plugin cache.

Unit (no live Terraform). The dominant per-turn cost is a full `terraform init` (~19s cold); on a
warm module (.terraform/ + lockfile present) init is skipped entirely, and a shared
TF_PLUGIN_CACHE_DIR is passed so even a cold init reuses downloaded providers.
"""

from __future__ import annotations

import pytest

from app.settings import Settings
from app.tools.terraform import TerraformRunner


def _module(tmp_path, initialized: bool):
    mod = tmp_path / "demo-null"
    mod.mkdir()
    if initialized:
        (mod / ".terraform").mkdir()
        (mod / ".terraform.lock.hcl").write_text("# lock")
    return mod


def test_is_initialized_reflects_marker_files(tmp_path):
    s = Settings(_env_file=None, terraform_workspaces_dir=str(tmp_path))
    _module(tmp_path, initialized=False)
    r = TerraformRunner("demo-null", s)
    assert r._is_initialized() is False
    (tmp_path / "demo-null" / ".terraform").mkdir()
    (tmp_path / "demo-null" / ".terraform.lock.hcl").write_text("x")
    assert r._is_initialized() is True


async def test_init_skipped_on_warm_module_without_running_terraform(tmp_path, monkeypatch):
    s = Settings(_env_file=None, terraform_workspaces_dir=str(tmp_path),
                 aegisops_tf_skip_init_when_ready=True)
    _module(tmp_path, initialized=True)
    r = TerraformRunner("demo-null", s)

    def _boom(*a, **k):
        raise AssertionError("warm init must be skipped — terraform must NOT be invoked")

    monkeypatch.setattr(r, "_console", _boom)
    out = await r.init()
    assert out["skipped_init"] is True


async def test_init_runs_when_flag_off(tmp_path, monkeypatch):
    s = Settings(_env_file=None, terraform_workspaces_dir=str(tmp_path),
                 aegisops_tf_skip_init_when_ready=False)
    _module(tmp_path, initialized=True)
    r = TerraformRunner("demo-null", s)
    ran = {}

    class _FakeConsole:
        async def run(self, args, on_line=None, timeout=None):
            ran["yes"] = True

            class _R:
                returncode = 0
                stderr: list = []

            return _R()

    monkeypatch.setattr(r, "_console", lambda include_ws=False: _FakeConsole())
    out = await r.init()
    assert ran.get("yes") and out["skipped_init"] is False


async def test_warm_skip_falls_back_to_full_init_when_unusable(tmp_path, monkeypatch):
    """The module claims initialized (.terraform/ + lockfile) but the provider cache is gone, so
    the warm path's ensure_state_workspace fails — init must FALL BACK to a full init, not fail."""
    s = Settings(_env_file=None, terraform_workspaces_dir=str(tmp_path),
                 aegisops_tf_skip_init_when_ready=True)
    _module(tmp_path, initialized=True)
    r = TerraformRunner("demo-null", s, state_workspace="res-x")

    from app.tools.terraform import TerraformError

    async def _bad_ensure():
        raise TerraformError("Required plugins are not installed")

    monkeypatch.setattr(r, "ensure_state_workspace", _bad_ensure)
    ran = {}

    class _FakeConsole:
        async def run(self, args, on_line=None, timeout=None):
            ran["yes"] = True

            class _R:
                returncode = 0
                stderr: list = []

            return _R()

    monkeypatch.setattr(r, "_console", lambda include_ws=False: _FakeConsole())
    # ensure_state_workspace is patched to fail once (warm) then the full-init path calls it again;
    # after fallback we replace it with a no-op so the full-init path completes.
    calls = {"n": 0}

    async def _ensure_seq():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TerraformError("Required plugins are not installed")
        return None

    monkeypatch.setattr(r, "ensure_state_workspace", _ensure_seq)
    out = await r.init()
    assert ran.get("yes"), "must fall back to a full terraform init"
    assert out["skipped_init"] is False


def test_plugin_cache_dir_threaded_into_env():
    s = Settings(_env_file=None, tf_plugin_cache_dir="/app/.tf-plugin-cache")
    env = TerraformRunner("demo-null", s)._env()
    assert env.get("TF_PLUGIN_CACHE_DIR") == "/app/.tf-plugin-cache"
    # off when the SETTING is empty — pinned explicitly: api-test itself now exports
    # TF_PLUGIN_CACHE_DIR (shared cache volume), and Settings(_env_file=None) still reads
    # process env, so the old bare-Settings form asserted on the ambient environment, not
    # on the runner's threading logic (deliberate test update, 2026-07-14).
    off = Settings(_env_file=None, tf_plugin_cache_dir="")
    assert "TF_PLUGIN_CACHE_DIR" not in TerraformRunner("demo-null", off)._env()
