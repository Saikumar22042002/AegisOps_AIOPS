"""A3 — unique per-run plan-file + remote-backend config plumbing.

Unit (no live Terraform). The plan-file must be unique per run so two operations (even two
creates of the same resource, or two concurrent runs in one module dir) never share/overwrite a
plan file; and `init` must supply the S3+DynamoDB `-backend-config` in remote mode only.
"""

from __future__ import annotations

from app.settings import Settings
from app.tools.terraform import TerraformRunner


def _settings(**kw):
    return Settings(_env_file=None, **kw)


def test_plan_file_is_unique_per_run():
    s = _settings()
    a = TerraformRunner("aws-ec2", s, state_workspace="res-web", run_id="run-A")
    b = TerraformRunner("aws-ec2", s, state_workspace="res-web", run_id="run-B")
    assert a.plan_file != b.plan_file, "two runs of the same resource must not share a plan file"
    assert "run-A" in a.plan_file and "run-B" in b.plan_file
    assert a.plan_file.endswith(".tfplan")


def test_plan_and_execute_reuse_the_same_plan_file():
    # plan (cloudops_plan) and apply (cloudops_execute) construct separate runners for the SAME
    # run — same state_workspace + run_id must yield the SAME plan-file path (apply reads it).
    s = _settings()
    planner = TerraformRunner("aws-ec2", s, state_workspace="res-web", run_id="run-1")
    executor = TerraformRunner("aws-ec2", s, state_workspace="res-web", run_id="run-1")
    assert planner.plan_file == executor.plan_file


def test_plan_file_falls_back_without_run_id():
    s = _settings()
    assert TerraformRunner("aws-ec2", s).plan_file == "aegisops.tfplan"
    assert TerraformRunner("aws-ec2", s, state_workspace="res-x").plan_file == "aegisops-res-x.tfplan"


def test_backend_config_empty_in_local_mode():
    s = _settings(aegisops_tf_backend="local", tf_state_bucket="my-bucket")
    r = TerraformRunner("aws-ec2", s, state_workspace="res-web", run_id="r1")
    assert r._backend_config_args() == [], "local mode must not inject a remote backend"


def test_backend_config_supplied_in_remote_mode():
    s = _settings(aegisops_tf_backend="remote", tf_state_bucket="aegis-tfstate",
                  tf_state_dynamodb_table="aegis-locks", tf_state_region="us-east-1",
                  tf_state_key_prefix="aegisops")
    r = TerraformRunner("aws-ec2", s, state_workspace="res-web", run_id="r1")
    args = r._backend_config_args()
    joined = " ".join(args)
    assert "-backend-config=bucket=aegis-tfstate" in args
    assert "-backend-config=key=aegisops/aws-ec2/res-web.tfstate" in args
    assert "-backend-config=region=us-east-1" in args
    assert "-backend-config=dynamodb_table=aegis-locks" in args, "state locking must be configured"
    assert "res-web" in joined  # state key is namespaced per module + per-resource workspace


def test_remote_backend_noop_without_bucket():
    # Remote requested but no bucket configured → no args (falls back to the module's declared
    # backend) rather than emitting an invalid partial config.
    s = _settings(aegisops_tf_backend="remote", tf_state_bucket="")
    r = TerraformRunner("aws-ec2", s, state_workspace="res-web", run_id="r1")
    assert r._backend_config_args() == []
