"""P1 provider/model substrate (Redesign/02 §9, 04 §4, 05 §11, 07 P1).

Canonical, provider-neutral LLM layer. Application code imports from here (or the
`agents/llm.py` compatibility shim); provider SDKs live only in `app/llm/adapters/`
(P1.9 import boundary). The P2 harness builds on these contracts — this package
deliberately contains NO reasoning loop, NO tool execution, NO memory: P1 routing is
deterministic model/provider selection + resilience, never Observe→Reason→Act.
"""

from .errors import ModelError
from .types import (
    GOVERNED_PURPOSES,
    PURPOSES,
    CanonicalMessage,
    ModelRequest,
    ModelResponse,
    RoutePlan,
    ServedBy,
    StreamEvent,
    ToolCall,
    ToolResult,
    Usage,
)

__all__ = [
    "GOVERNED_PURPOSES",
    "PURPOSES",
    "CanonicalMessage",
    "ModelError",
    "ModelRequest",
    "ModelResponse",
    "RoutePlan",
    "ServedBy",
    "StreamEvent",
    "ToolCall",
    "ToolResult",
    "Usage",
]
