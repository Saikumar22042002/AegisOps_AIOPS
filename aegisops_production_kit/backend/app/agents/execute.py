"""Execute dispatcher — routes the post-approval execution to the right domain executor."""

from __future__ import annotations

from .cloudops import cloudops_execute
from .devops import devops_execute
from .sre import sre_execute
from .state import AgentState


async def execute(state: AgentState, config) -> dict:
    domain = state.get("domain")
    if domain == "cloudops":
        return await cloudops_execute(state, config)
    if domain == "devops":
        return await devops_execute(state, config)
    if domain == "sre":
        return await sre_execute(state, config)
    return {}
