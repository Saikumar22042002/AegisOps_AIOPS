"""P2.1 — Tool Registry v2 foundation: native schemas + read-only execution (05 §1/§3).

The registry offers frozen read tools as native-FC schemas and executes a model-issued
ToolCall back through them, never raising — a failed/unknown/timed-out/mis-argued tool is
a typed ToolResult the loop turns into an observation (L3). Read-only-by-construction is
inherited from the investigation registry (mutation-marker names refused at registration).
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.investigation import ReadOnlyViolation, ToolRegistry
from app.harness import registry as reg2
from app.llm.types import ToolCall

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _reg(**tools) -> ToolRegistry:
    r = ToolRegistry()
    for name, fn in tools.items():
        r.register(name, f"{name} desc", fn)
    return r.freeze()


def test_schemas_are_native_tooldefs_for_every_read_tool():
    r = _reg(list_pods=lambda **_: 1, query_prometheus=lambda **_: 2)
    schemas = reg2.schemas_for(r)
    names = {s["name"] for s in schemas}
    assert names == {"list_pods", "query_prometheus"}
    assert all(set(s) == {"name", "description", "input_schema"} for s in schemas)
    assert all(s["input_schema"]["type"] == "object" for s in schemas)


def test_registry_stays_read_only_by_construction():
    r = ToolRegistry()
    with pytest.raises(ReadOnlyViolation):
        r.register("delete_bucket", "danger", lambda **_: 1)   # mutation marker refused


async def test_execute_success_returns_ok_observation():
    async def pods(**_):
        return {"pods": ["a", "b"]}
    r = _reg(list_pods=pods)
    res = await reg2.execute(r, ToolCall(id="1", name="list_pods", arguments={"ns": "p"}))
    assert res.ok and "pods" in res.content and res.stage == "observe"


async def test_execute_redacts_secrets_in_result():
    async def creds(**_):
        return {"token": "sk-live-DEADBEEF", "user": "svc"}
    r = _reg(read_creds=creds)
    res = await reg2.execute(r, ToolCall(id="1", name="read_creds"))
    assert res.ok and "sk-live-DEADBEEF" not in res.content


async def test_unknown_tool_is_typed_not_raised():
    r = _reg(look=lambda **_: 1)
    res = await reg2.execute(r, ToolCall(id="1", name="nope"))
    assert not res.ok and res.error["kind"] == "unknown_tool" and res.stage == "policy_verdict"


async def test_tool_exception_becomes_typed_failure():
    async def boom(**_):
        raise RuntimeError("down")
    r = _reg(look=boom)
    res = await reg2.execute(r, ToolCall(id="1", name="look"))
    assert not res.ok and res.error["kind"] == "tool_error"


async def test_bad_arguments_become_typed_failure():
    async def needs_ns(ns):        # required positional the model omitted
        return ns
    r = _reg(look=needs_ns)
    res = await reg2.execute(r, ToolCall(id="1", name="look", arguments={"wrong": 1}))
    assert not res.ok and res.error["kind"] == "bad_arguments"


async def test_timeout_becomes_typed_failure():
    async def slow(**_):
        await asyncio.sleep(1.0)
    r = _reg(look=slow)
    res = await reg2.execute(r, ToolCall(id="1", name="look"), timeout_s=0.05)
    assert not res.ok and res.error["kind"] == "timeout" and res.stage == "timeout"
