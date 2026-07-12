"""Phase-A LIVE-CLOUD safety invariants (03_TEST_MATRIX §A, live tier).

Runs the destructive-class invariants against REAL AWS using cheap S3 buckets:
  A1/A2 — two sequential creates coexist (distinct state, both buckets really exist);
  A3    — destroying one removes only it.

Gated behind AEGISOPS_TEST_LIVE_CLOUD=1 + AWS creds; everything created here is destroyed in
teardown. Skips cleanly otherwise.
"""

from __future__ import annotations

import os
import uuid

import pytest

_LIVE = os.getenv("AEGISOPS_TEST_LIVE_CLOUD") == "1" and bool(os.getenv("AWS_ACCESS_KEY_ID"))

pytestmark = pytest.mark.skipif(
    not _LIVE, reason="live-cloud tier: set AEGISOPS_TEST_LIVE_CLOUD=1 with AWS creds")


async def _quiet(stream, line):
    pass


@pytest.fixture
async def runners():
    from app.settings import get_settings
    from app.tools.terraform import TerraformRunner

    settings = get_settings()
    tag = uuid.uuid4().hex[:8]
    names = [f"aegisops-inv-{tag}-x", f"aegisops-inv-{tag}-y"]
    rs = [TerraformRunner("aws-s3", settings, state_workspace=f"res-{n}") for n in names]
    yield names, rs
    for r, n in zip(rs, names):
        try:
            await r.destroy({"bucket_name": n, "region": "us-east-1"}, _quiet)
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass


async def test_create_never_destroys_and_both_coexist_live(runners):
    from app.settings import get_settings
    from app.tools import aws as aws_tool

    (name_x, name_y), (rx, ry) = runners
    reader = aws_tool.get_aws(get_settings())

    # Create X.
    await rx.init(_quiet)
    await rx.ensure_state_workspace()
    px = await rx.plan({"bucket_name": name_x, "region": "us-east-1"}, on_line=_quiet)
    assert px["summary"]["destroy"] == 0
    await rx.apply(_quiet)
    assert await reader.bucket_taken(name_x) is True

    # Create Y — the plan must not contain a single destroy, and X must survive the apply.
    await ry.ensure_state_workspace()
    py = await ry.plan({"bucket_name": name_y, "region": "us-east-1"}, on_line=_quiet)
    assert py["summary"]["destroy"] == 0, "A1 violated: create plan wanted to destroy"
    assert all("delete" not in rc["actions"] for rc in py["diff"])
    await ry.apply(_quiet)

    assert await reader.bucket_taken(name_x) is True, "A2 violated: creating Y destroyed X"
    assert await reader.bucket_taken(name_y) is True

    # A3 — destroy X only; Y survives.
    await rx.destroy({"bucket_name": name_x, "region": "us-east-1"}, _quiet)
    assert await reader.bucket_taken(name_y) is True, "A3 violated: destroy removed the wrong resource"
    assert not await rx.state_list()
    assert await ry.state_list()
